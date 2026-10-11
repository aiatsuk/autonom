#!/usr/bin/env node
/**
 * Fake MCP Apps host harness for the Autonom ChatGPT probe (contract section 7.2).
 *
 * Not part of `node --test tests/*.test.mjs`. It starts the real probe server through
 * `P/bin/launch.sh` (P = integrations/chatgpt/autonom-chatgpt-probe) with a temporary AUTONOM_HOME,
 * speaks MCP to it over stdio, serves a host page at http://127.0.0.1:<H>/ and the probe HTML at
 * http://localhost:<I>/probe.html (a different origin) with a Content-Security-Policy header built
 * from the resource `_meta.ui.csp`, and drives the page in headless Chromium and WebKit through
 * Playwright (PLAYWRIGHT_ROOT, default /Users/gmh-basket/pr/defuddle-eval):
 *
 *   node tests/live/chatgpt_probe_host_live.mjs --evidence-dir DIR [--browser chromium|webkit|both]
 *        [--remote-proxy | --ssh-forward HOST]
 *        [--device android:emulator-5584] [--device ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5]
 *   node tests/live/chatgpt_probe_host_live.mjs --dry-run [...]     (prints the plan, starts nothing)
 *   node tests/live/chatgpt_probe_host_live.mjs --self-test          (pure helpers only)
 *
 * The host page implements the host side of the MCP Apps bridge: it answers ui/initialize
 * (hostInfo autonom-fake-host 0.1.0, a hostContext with theme, displayMode fullscreen,
 * containerDimensions and platform desktop), sends ui/notifications/tool-input and then
 * ui/notifications/tool-result for open_workspace after ui/notifications/initialized, relays
 * tools/call to the server, records ui/update-model-context and ui/message, and ends each
 * browser run with a ui/resource-teardown request.
 *
 * --remote-proxy starts an in-process TCP proxy 127.0.0.1:N -> media port and declares it as remote
 * route `proxy` ("forwarded (in-process proxy)": a second declared loopback origin, not a remote
 * host). --ssh-forward HOST runs `ssh -o BatchMode=yes -o ConnectTimeout=5 -o ExitOnForwardFailure=yes
 * -N -L N:127.0.0.1:<media port> HOST`; when the forward does not listen within 10 s the remote route
 * is not declared and every remote check is not_exercised with "ssh forward unavailable: ...". The
 * ssh process is killed at the end; ssh and system settings are never changed.
 *
 * Devices: only android:emulator-5584 and ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5 are accepted;
 * anything else (android:emulator-5554 above all) is refused before any process starts. The
 * harness never boots a device; with --device it clicks *Run device checks* once.
 *
 * Writes into DIR, per requested browser: results-<browser>.json, screenshots/<browser>-narrow.png,
 * screenshots/<browser>-wide.png and host-log-<browser>.json (bridge messages without the token).
 * A browser that is not installed under PLAYWRIGHT_ROOT still gets a results file in which every
 * check is not_exercised with "<browser> not installed under PLAYWRIGHT_ROOT".
 *
 * Exit 0 when every requested browser has a results file, the Chromium file passes
 * `probe_results.py check --profile harness` and the WebKit file passes the base check
 * (WebKit statuses are recorded, not judged). Exit 1 otherwise, 2 on a usage error.
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const PLUGIN = join(ROOT, "integrations", "chatgpt", "autonom-chatgpt-probe");
const LAUNCHER = join(PLUGIN, "bin", "launch.sh");
const CORE = join(PLUGIN, "ui", "probe-core.mjs");
const CHECKER = join(ROOT, "integrations", "chatgpt", "scripts", "probe_results.py");
const AUTONOM_CLI = join(ROOT, "scripts", "autonom.py");
const DEFAULT_PLAYWRIGHT_ROOT = "/Users/gmh-basket/pr/defuddle-eval";

export const HOST_INFO = Object.freeze({ name: "autonom-fake-host", version: "0.1.0" });
export const UI_PROTOCOL = "2026-01-26";
export const MCP_PROTOCOL = "2025-11-25";
export const UI_URI = "ui://autonom-probe/probe.html";
export const UI_MIME = "text/html;profile=mcp-app";
export const ALLOWED_DEVICES = Object.freeze(["android:emulator-5584", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"]);
export const BROWSERS = Object.freeze(["chromium", "webkit"]);
export const VIEWPORTS = Object.freeze({ narrow: { width: 432, height: 780 }, wide: { width: 1200, height: 820 } });
export const PROXY_LABEL = "proxy";
export const PROXY_KIND = "forwarded (in-process proxy)";
export const SSH_LABEL = "forwarded";
const RUN_ALL_CAP_MS = 90_000;
const DEVICE_CAP_MS = 120_000;
const SSH_WAIT_MS = 10_000;
const LOG_STRING_MAX = 200;
const SSH_HOST = /^[A-Za-z0-9][A-Za-z0-9._@-]{0,252}$/;

const USAGE = "usage: node tests/live/chatgpt_probe_host_live.mjs --evidence-dir DIR [--browser chromium|webkit|both] " +
  "[--remote-proxy | --ssh-forward HOST] [--device SPEC]... | --dry-run [...] | --self-test";

// ------------------------------------------------------------------------------------ arguments

export class UsageError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

/** Parses and validates the command line. Throws UsageError before anything starts. */
export function parseCli(argv) {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      options: {
        "evidence-dir": { type: "string" },
        browser: { type: "string" },
        "remote-proxy": { type: "boolean" },
        "ssh-forward": { type: "string" },
        device: { type: "string", multiple: true },
        "dry-run": { type: "boolean" },
        "self-test": { type: "boolean" },
        help: { type: "boolean" },
      },
      allowPositionals: false,
      strict: true,
    }).values;
  } catch (error) {
    throw new UsageError("invalid_arguments", error.message);
  }
  const browser = parsed.browser ?? "chromium";
  if (!["chromium", "webkit", "both"].includes(browser)) {
    throw new UsageError("invalid_arguments", `--browser must be chromium, webkit or both, got ${browser}`);
  }
  if (parsed["remote-proxy"] && parsed["ssh-forward"] !== undefined) {
    throw new UsageError("invalid_arguments", "--remote-proxy and --ssh-forward cannot be combined");
  }
  const sshHost = parsed["ssh-forward"];
  if (sshHost !== undefined && !SSH_HOST.test(sshHost)) {
    throw new UsageError("invalid_arguments", "--ssh-forward needs a plain host name (letters, digits, . _ @ -; not starting with -)");
  }
  const devices = [];
  for (const spec of parsed.device ?? []) {
    if (!ALLOWED_DEVICES.includes(spec)) {
      throw new UsageError("device_not_allowed", `--device ${spec} is not allowed; only ${ALLOWED_DEVICES.join(" and ")}`);
    }
    if (!devices.includes(spec)) devices.push(spec);
  }
  const mode = parsed["self-test"] ? "self-test" : parsed["dry-run"] ? "dry-run" : parsed.help ? "help" : "run";
  const evidenceDir = parsed["evidence-dir"] ? resolve(parsed["evidence-dir"]) : null;
  if (mode === "run" && !evidenceDir) throw new UsageError("invalid_arguments", "--evidence-dir is required");
  return {
    mode,
    evidenceDir,
    browsers: browser === "both" ? [...BROWSERS] : [browser],
    remote: parsed["remote-proxy"] ? { kind: "proxy" } : sshHost !== undefined ? { kind: "ssh", host: sshHost } : { kind: "none" },
    devices,
  };
}

// ------------------------------------------------------------------------------------ pure helpers

/** REF-5 default CSP plus the resource's declared domains (connect-src from connectDomains). */
export function buildCsp(csp = {}) {
  const list = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === "string" && /^[^\s;,'"]+$/.test(item)) : []);
  const connect = list(csp.connectDomains);
  const resources = list(csp.resourceDomains);
  const frames = list(csp.frameDomains);
  const bases = list(csp.baseUriDomains);
  const extra = resources.length ? ` ${resources.join(" ")}` : "";
  const parts = [
    "default-src 'none'",
    `script-src 'self' 'unsafe-inline'${extra}`,
    `style-src 'self' 'unsafe-inline'${extra}`,
    `img-src 'self' data:${extra}`,
    `media-src 'self' data:${extra}`,
    `connect-src ${connect.length ? connect.join(" ") : "'none'"}`,
  ];
  if (resources.length) parts.push(`font-src${extra}`);
  if (frames.length) parts.push(`frame-src ${frames.join(" ")}`);
  if (bases.length) parts.push(`base-uri ${bases.join(" ")}`);
  return `${parts.join("; ")};`;
}

/** Last `max` characters of a text, with every secret replaced. */
export function tail(text, max = 200, secrets = []) {
  let out = String(text ?? "");
  for (const secret of secrets) if (typeof secret === "string" && secret.length >= 4) out = out.split(secret).join("[redacted]");
  return out.length > max ? out.slice(out.length - max) : out;
}

/**
 * Deep copy for evidence: every secret replaced by [redacted], any "bearer." neutralised, keys named
 * like a token dropped, and long strings (frame bytes, HTML) shortened to a length marker.
 */
export function scrubDeep(value, secrets = [], maxString = LOG_STRING_MAX) {
  const list = secrets.filter((s) => typeof s === "string" && s.length >= 4).sort((a, b) => b.length - a.length);
  const clean = (text) => {
    let out = text;
    for (const secret of list) out = out.split(secret).join("[redacted]");
    out = out.replace(/bearer\./gi, "bearer_");
    return out.length > maxString ? `<${out.length} chars>` : out;
  };
  const walk = (v) => {
    if (typeof v === "string") return clean(v);
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      const out = {};
      for (const [key, inner] of Object.entries(v)) {
        if (["media_token", "token", "authorization"].includes(key.toLowerCase())) {
          out[key] = "[redacted]";
          continue;
        }
        out[clean(key)] = walk(inner);
      }
      return out;
    }
    return v;
  };
  return walk(value);
}

/** True when any secret (or "bearer.") appears anywhere in the JSON text of `value`. */
export function containsSecret(value, secrets = []) {
  const text = JSON.stringify(value);
  if (/bearer\./i.test(text)) return true;
  return secrets.some((secret) => typeof secret === "string" && secret.length >= 4 && text.includes(secret));
}

/** The probe config the harness writes into the temporary AUTONOM_HOME. */
export function probeConfig({ mediaPort, iframeOrigin, remoteRoute, devices, node }) {
  return {
    media_port: mediaPort,
    allow_origins: [iframeOrigin],
    remote_routes: remoteRoute ? [remoteRoute] : [],
    allowed_devices: [...devices],
    autonom_root: ROOT,
    node,
  };
}

/** ssh argument array for --ssh-forward (contract 7.2); the host was validated by parseCli. */
export function sshArgv(host, localPort, mediaPort) {
  return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ExitOnForwardFailure=yes",
    "-N", "-L", `${localPort}:127.0.0.1:${mediaPort}`, host];
}

/** hostContext the fake host reports for a viewport. */
export function hostContext(viewport, theme = "light") {
  return {
    theme,
    displayMode: "fullscreen",
    availableDisplayModes: ["inline", "fullscreen"],
    containerDimensions: { width: viewport.width, height: viewport.height },
    platform: "desktop",
    locale: "en-US",
    toolInfo: { tool: { name: "open_workspace" } },
  };
}

/**
 * Host side of the MCP Apps bridge. `handle(message)` takes one JSON-RPC message from the View and
 * returns the messages to post back, in order. `callTool(params)` relays tools/call to the server and
 * resolves to {result} or {error}.
 */
export class HostBridge {
  constructor({ callTool, openResult, context, log = () => {} }) {
    this.callTool = callTool;
    this.openResult = openResult;
    this.context = context;
    this.log = log;
    this.modelContexts = [];
    this.chatMessages = [];
    this.initialized = false;
    this.viewAnswers = [];
  }

  async handle(message) {
    if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") return [];
    this.log("view->host", message);
    const out = [];
    const hasId = typeof message.id === "number" || typeof message.id === "string";
    if (typeof message.method !== "string") {
      if (hasId) this.viewAnswers.push(message);
      return out;
    }
    if (!hasId) {
      if (message.method === "ui/notifications/initialized") {
        this.initialized = true;
        out.push({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: {} } });
        out.push({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: this.openResult });
      }
      for (const item of out) this.log("host->view", item);
      return out;
    }
    const reply = (body) => ({ jsonrpc: "2.0", id: message.id, ...body });
    const params = message.params && typeof message.params === "object" ? message.params : {};
    switch (message.method) {
      case "ui/initialize":
        out.push(reply({
          result: {
            protocolVersion: UI_PROTOCOL,
            hostInfo: { ...HOST_INFO },
            hostCapabilities: { serverTools: {}, updateModelContext: { text: {} }, message: { text: {} }, logging: {} },
            hostContext: this.context,
          },
        }));
        break;
      case "tools/call": {
        let answer;
        try {
          answer = await this.callTool(params);
        } catch (error) {
          answer = { error: { code: -32603, message: String(error && error.message ? error.message : error).slice(0, 200) } };
        }
        out.push(reply(answer && answer.error ? { error: answer.error } : { result: answer ? answer.result : {} }));
        break;
      }
      case "ui/update-model-context":
        this.modelContexts.push(params);
        out.push(reply({ result: {} }));
        break;
      case "ui/message":
        this.chatMessages.push(params);
        out.push(reply({ result: {} }));
        break;
      case "ui/request-display-mode":
        out.push(reply({ result: { mode: "fullscreen" } }));
        break;
      case "ui/resource-teardown":
      case "ping":
        out.push(reply({ result: {} }));
        break;
      default:
        out.push(reply({ error: { code: -32601, message: "Method not found", data: { method: message.method } } }));
    }
    for (const item of out) this.log("host->view", item);
    return out;
  }

  /** True when a recorded ui/update-model-context carries the nonce. */
  contextHasNonce(nonce) {
    return Boolean(nonce) && this.modelContexts.some((params) => JSON.stringify(params).includes(nonce));
  }
}

/** Results for a browser that could not run: every check not_exercised with `reason` (probe-core resultsJson). */
export function stubResults(core, reason, { localOrigin = null } = {}) {
  const run = core.createRun();
  for (const id of core.CHECK_IDS) core.setStatus(run, id, { status: "not_exercised", reason, duration_ms: null });
  core.finishRun(run);
  return core.resultsJson(run, {
    host: { name: HOST_INFO.name, version: HOST_INFO.version, protocol: null, entry: "unknown" },
    routes: { local: { origin: localOrigin }, remote: { origin: null, label: null, selected: false } },
  });
}

/** When the ssh forward never came up, every remote check that was not exercised gets the ssh reason. */
export function annotateSshUnavailable(results, reason) {
  for (const check of results.checks || []) {
    if (check.route === "remote" && check.status === "not_exercised") check.reason = reason;
  }
  return results;
}

/** The plan printed by --dry-run: what would start and which files would be written. */
export function planFor(opts, { playwrightRoot = process.env.PLAYWRIGHT_ROOT || DEFAULT_PLAYWRIGHT_ROOT } = {}) {
  const entry = join(playwrightRoot, "node_modules", "playwright", "index.mjs");
  const dir = opts.evidenceDir || "<evidence-dir>";
  const remote = opts.remote.kind === "proxy"
    ? { kind: "proxy", label: PROXY_LABEL, evidence_label: PROXY_KIND, origin: "http://127.0.0.1:<proxy port>" }
    : opts.remote.kind === "ssh"
      ? { kind: "ssh", label: SSH_LABEL, origin: "http://127.0.0.1:<forward port>", argv: sshArgv(opts.remote.host, "<forward port>", "<media port>") }
      : { kind: "none" };
  const files = [];
  for (const browser of opts.browsers) {
    files.push(join(dir, `results-${browser}.json`), join(dir, "screenshots", `${browser}-narrow.png`),
      join(dir, "screenshots", `${browser}-wide.png`), join(dir, `host-log-${browser}.json`));
  }
  return {
    ok: true,
    dry_run: true,
    browsers: opts.browsers,
    playwright: { root: playwrightRoot, entry, found: existsSync(entry) },
    server: { launcher: LAUNCHER, cwd: PLUGIN, autonom_cli: AUTONOM_CLI, autonom_root: ROOT, autonom_home: "<temporary directory>" },
    host_page: "http://127.0.0.1:<host port>/",
    probe_page: "http://localhost:<iframe port>/probe.html",
    media_port: "<free port>",
    remote,
    devices: opts.devices,
    viewports: VIEWPORTS,
    steps: [
      "pick free ports; start the host page and the probe page servers on 127.0.0.1",
      remote.kind === "proxy" ? "start the in-process TCP proxy to the media port" : remote.kind === "ssh" ? "start the ssh forward and wait up to 10 s" : "no remote route",
      "write config.json into a temporary AUTONOM_HOME",
      "start the probe server through bin/launch.sh; initialize, tools/list, resources/read",
      "per browser: call open_workspace, load the host page, click Run all (90 s cap), answer bridge.context",
      opts.devices.length ? "click Run device checks once" : "no device checks",
      "save narrow and wide screenshots, results and the host log; send ui/resource-teardown",
      "check the Chromium results with --profile harness and the WebKit results with the base check",
      "stop the browser, server, media child, proxy or ssh, and remove the temporary AUTONOM_HOME",
    ],
    files,
  };
}

// ------------------------------------------------------------------------------------ self-test

async function selfTest() {
  let count = 0;
  const failures = [];
  const ok = (cond, label) => {
    count += 1;
    if (!cond) failures.push(label);
  };
  const throwsCode = (fn, code, label) => {
    try {
      fn();
      ok(false, `${label}: no error`);
    } catch (error) {
      ok(error instanceof UsageError && error.code === code, `${label}: ${error.code} ${error.message}`);
    }
  };

  // arguments
  const base = parseCli(["--evidence-dir", "/tmp/x"]);
  ok(base.mode === "run" && base.browsers.join() === "chromium" && base.remote.kind === "none", "default run");
  ok(parseCli(["--dry-run", "--browser", "both", "--remote-proxy"]).browsers.join() === "chromium,webkit", "both browsers");
  ok(parseCli(["--dry-run", "--ssh-forward", "localhost"]).remote.host === "localhost", "ssh host kept");
  ok(parseCli(["--dry-run", "--device", ALLOWED_DEVICES[0], "--device", ALLOWED_DEVICES[0]]).devices.length === 1, "devices deduplicated");
  throwsCode(() => parseCli([]), "invalid_arguments", "evidence dir required");
  throwsCode(() => parseCli(["--dry-run", "--browser", "firefox"]), "invalid_arguments", "bad browser");
  throwsCode(() => parseCli(["--dry-run", "--remote-proxy", "--ssh-forward", "h"]), "invalid_arguments", "remote flags exclusive");
  throwsCode(() => parseCli(["--dry-run", "--ssh-forward", "-oProxyCommand=x"]), "invalid_arguments", "ssh option injection");
  throwsCode(() => parseCli(["--dry-run", "--ssh-forward", "a b"]), "invalid_arguments", "ssh host with space");
  throwsCode(() => parseCli(["--dry-run", "--device", "android:emulator-5554"]), "device_not_allowed", "emulator-5554 refused");
  throwsCode(() => parseCli(["--dry-run", "--device", "android:emulator-5586"]), "device_not_allowed", "other device refused");
  throwsCode(() => parseCli(["--dry-run", "--bogus"]), "invalid_arguments", "unknown flag");
  throwsCode(() => parseCli(["--dry-run", "extra"]), "invalid_arguments", "positional refused");

  // CSP
  const csp = buildCsp({ connectDomains: ["http://127.0.0.1:47811", "ws://127.0.0.1:47811", "bad value;"], resourceDomains: [] });
  ok(csp === "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
    "media-src 'self' data:; connect-src http://127.0.0.1:47811 ws://127.0.0.1:47811;", `csp with domains: ${csp}`);
  ok(buildCsp({}).includes("connect-src 'none'"), "csp without domains");
  ok(buildCsp({ resourceDomains: ["https://cdn.example"], frameDomains: ["https://f.example"] }).includes("frame-src https://f.example"), "csp frames");

  // scrubbing
  const token = "tok_0123456789abcdefghijklmnopqrstuvwxyz";
  const scrubbed = scrubDeep({ a: `x ${token} y`, b: [`bearer.${token}`], media_token: token, long: "z".repeat(500), n: 3 }, [token]);
  ok(!containsSecret(scrubbed, [token]), "scrubbed has no secret");
  ok(scrubbed.a === "x [redacted] y" && scrubbed.media_token === "[redacted]" && scrubbed.long === "<500 chars>" && scrubbed.n === 3, "scrub shape");
  ok(containsSecret({ a: token }, [token]) && containsSecret({ a: "Bearer.x" }), "containsSecret detects");
  ok(tail(`abc${token}`, 200, [token]) === "abc[redacted]" && tail("0123456789", 4) === "6789", "tail");

  // ssh argv and config
  const argv = sshArgv("localhost", 50001, 50000);
  ok(argv.join(" ") === "ssh -o BatchMode=yes -o ConnectTimeout=5 -o ExitOnForwardFailure=yes -N -L 50001:127.0.0.1:50000 localhost", "ssh argv");
  const config = probeConfig({ mediaPort: 50000, iframeOrigin: "http://localhost:50002", remoteRoute: { label: "proxy", origin: "http://127.0.0.1:50001" }, devices: ["ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"], node: "/usr/bin/node" });
  ok(config.media_port === 50000 && config.allow_origins[0] === "http://localhost:50002" && config.remote_routes.length === 1 && config.autonom_root === ROOT, "config");

  // host bridge
  const calls = [];
  const openResult = { structuredContent: { ok: true }, _meta: { "autonom/probe": { media_token: token, context_nonce: "probe-0abc-lime" } } };
  const bridge = new HostBridge({
    callTool: async (params) => {
      calls.push(params);
      return params.name === "boom" ? { error: { code: -32602, message: "Unknown tool: boom" } } : { result: { isError: false, structuredContent: { ok: true } } };
    },
    openResult,
    context: hostContext(VIEWPORTS.narrow),
  });
  const init = await bridge.handle({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: {} });
  ok(init.length === 1 && init[0].id === 1 && init[0].result.hostInfo.name === "autonom-fake-host" &&
    init[0].result.protocolVersion === UI_PROTOCOL && init[0].result.hostContext.displayMode === "fullscreen" &&
    init[0].result.hostContext.containerDimensions.width === 432, "ui/initialize answer");
  const after = await bridge.handle({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
  ok(after.length === 2 && after[0].method === "ui/notifications/tool-input" && after[1].method === "ui/notifications/tool-result" &&
    after[1].params === openResult && bridge.initialized, "tool-input then tool-result after initialized");
  const tool = await bridge.handle({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "probe_read", arguments: {} } });
  ok(tool[0].id === 2 && tool[0].result.structuredContent.ok === true && calls[0].name === "probe_read", "tools/call relayed");
  const toolError = await bridge.handle({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "boom" } });
  ok(toolError[0].error.code === -32602 && !("result" in toolError[0]), "tools/call error relayed");
  const thrower = new HostBridge({ callTool: async () => { throw new Error("server gone"); }, openResult, context: {} });
  const thrown = await thrower.handle({ jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "x" } });
  ok(thrown[0].error.code === -32603 && thrown[0].error.message === "server gone", "tools/call exception mapped");
  await bridge.handle({ jsonrpc: "2.0", id: 4, method: "ui/update-model-context", params: { content: [{ type: "text", text: "nonce probe-0abc-lime" }] } });
  ok(bridge.contextHasNonce("probe-0abc-lime") && !bridge.contextHasNonce("probe-ffff-fig") && !bridge.contextHasNonce(null), "model context recorded");
  const message = await bridge.handle({ jsonrpc: "2.0", id: 5, method: "ui/message", params: { role: "user", content: [] } });
  ok(message[0].result && bridge.chatMessages.length === 1, "ui/message recorded");
  const unknown = await bridge.handle({ jsonrpc: "2.0", id: 6, method: "ui/open-link", params: {} });
  ok(unknown[0].error.code === -32601, "unknown method");
  const teardown = await bridge.handle({ jsonrpc: "2.0", id: 7, method: "ui/resource-teardown", params: {} });
  ok(teardown[0].result && Object.keys(teardown[0].result).length === 0, "teardown answered");
  ok((await bridge.handle({ jsonrpc: "1.0", id: 8, method: "x" })).length === 0 && (await bridge.handle(null)).length === 0, "non JSON-RPC ignored");
  await bridge.handle({ jsonrpc: "2.0", id: 1, result: {} });
  ok(bridge.viewAnswers.length === 1, "view answers recorded");

  // stub results through probe-core
  const core = await loadCore();
  const stub = stubResults(core, "webkit not installed under PLAYWRIGHT_ROOT", { localOrigin: "http://127.0.0.1:50000" });
  ok(stub.checks.length === 27 && stub.checks.every((c) => c.status === "not_exercised" && c.reason === "webkit not installed under PLAYWRIGHT_ROOT"), "stub checks");
  ok(stub.summary.not_exercised === 27 && stub.summary.pass === 0 && core.RUN_ID_PATTERN.test(stub.run_id) && stub.routes.remote.selected === false, "stub summary");
  const annotated = annotateSshUnavailable(JSON.parse(JSON.stringify(stub)), "ssh forward unavailable: refused");
  ok(annotated.checks.filter((c) => c.route === "remote").length === 7 &&
    annotated.checks.filter((c) => c.route === "remote").every((c) => c.reason === "ssh forward unavailable: refused") &&
    annotated.checks.filter((c) => c.route !== "remote").every((c) => c.reason === "webkit not installed under PLAYWRIGHT_ROOT"), "ssh annotation");

  // plan
  const plan = planFor(parseCli(["--dry-run", "--browser", "both", "--ssh-forward", "localhost", "--device", ALLOWED_DEVICES[1]]), { playwrightRoot: "/nonexistent" });
  ok(plan.dry_run && plan.files.length === 8 && plan.remote.argv.at(-1) === "localhost" && plan.playwright.found === false && plan.devices.length === 1, "plan");

  if (failures.length) {
    process.stderr.write(`self-test FAILED (${failures.length} of ${count}):\n${failures.map((f) => `  - ${f}`).join("\n")}\n`);
    return 1;
  }
  process.stdout.write(`self-test ok (${count} assertions)\n`);
  return 0;
}

async function loadCore() {
  return await import(pathToFileURL(CORE).href);
}

// ------------------------------------------------------------------------------------ live run

function freePort() {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolvePort(port));
    });
  });
}

function listen(server, port, host) {
  return new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      server.off("error", reject);
      resolveListen(server.address().port);
    });
  });
}

function closeServer(server) {
  return new Promise((resolveClose) => {
    if (!server || !server.listening) return resolveClose();
    if (typeof server.closeAllConnections === "function") server.closeAllConnections();
    server.close(() => resolveClose());
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function canConnect(port) {
  return new Promise((resolveConnect) => {
    const socket = net.connect({ port, host: "127.0.0.1" });
    const done = (value) => {
      socket.destroy();
      resolveConnect(value);
    };
    socket.setTimeout(500, () => done(false));
    socket.once("connect", () => done(true));
    socket.once("error", () => done(false));
  });
}

function readBody(req, limit = 8 * 1024 * 1024) {
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("body too large"));
        req.destroy();
      } else chunks.push(chunk);
    });
    req.on("end", () => resolveBody(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

/** MCP client over the server's stdio: one JSON-RPC message per line. */
class McpClient {
  constructor(proc) {
    this.proc = proc;
    this.nextId = 1;
    this.pending = new Map();
    this.stderr = "";
    this.closed = false;
    createInterface({ input: proc.stdout }).on("line", (line) => this.#onLine(line));
    proc.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk.toString("utf8")).slice(-4000);
    });
    proc.on("exit", () => {
      this.closed = true;
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer);
        reject(new Error("probe server exited"));
      }
      this.pending.clear();
    });
  }

  #onLine(line) {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return;
    }
    if (msg && (typeof msg.id === "number") && this.pending.has(msg.id)) {
      const { resolve: done, timer } = this.pending.get(msg.id);
      clearTimeout(timer);
      this.pending.delete(msg.id);
      done(msg);
    }
  }

  request(method, params, timeoutMs = 30_000) {
    if (this.closed) return Promise.reject(new Error("probe server exited"));
    const id = this.nextId++;
    return new Promise((done, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: no answer within ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve: done, reject, timer });
      this.proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  notify(method, params) {
    if (!this.closed) this.proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
  }

  async stop() {
    if (this.closed) return;
    this.proc.stdin.end();
    for (let i = 0; i < 50 && !this.closed; i += 1) await sleep(100);
    if (!this.closed) this.proc.kill("SIGKILL");
  }
}

function hostPageHtml(probeUrl) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Autonom fake MCP Apps host</title>
<style>html,body{margin:0;height:100%;background:#7f7f7f}iframe{border:0;width:100vw;height:100vh;display:block}</style>
</head><body>
<iframe id="probe-frame" title="Autonom probe" src="${probeUrl}" sandbox="allow-scripts allow-same-origin allow-forms" allow="clipboard-write"></iframe>
<script>
const frame = document.getElementById("probe-frame");
const target = new URL(frame.src).origin;
const post = (message) => frame.contentWindow.postMessage(message, target);
window.__hostPost = post;
window.addEventListener("message", async (event) => {
  if (event.source !== frame.contentWindow || event.origin !== target) return;
  let out = [];
  try {
    const response = await fetch("/bridge", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(event.data) });
    out = await response.json();
  } catch (error) {
    out = [];
  }
  for (const message of out) post(message);
});
</script>
</body></html>
`;
}

async function loadPlaywright(root) {
  const entry = join(root, "node_modules", "playwright", "index.mjs");
  if (!existsSync(entry)) return null;
  return await import(pathToFileURL(entry).href);
}

function writeJson(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
}

function runChecker(args) {
  const out = spawnSync("python3", [CHECKER, "check", "--json", ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  let parsed = null;
  try {
    parsed = JSON.parse(out.stdout);
  } catch {
    parsed = { ok: false, error: tail(`${out.stdout}${out.stderr}`, 400) };
  }
  return { exit_code: out.status, ...parsed };
}

async function liveRun(opts) {
  const core = await loadCore();
  const playwrightRoot = process.env.PLAYWRIGHT_ROOT || DEFAULT_PLAYWRIGHT_ROOT;
  const secrets = [];
  const cleanups = [];
  const report = { ok: false, evidence_dir: opts.evidenceDir, browsers: {}, remote: { kind: opts.remote.kind }, devices: opts.devices };
  const cleanup = async () => {
    while (cleanups.length) {
      const fn = cleanups.pop();
      try {
        await fn();
      } catch {
        // keep cleaning up
      }
    }
  };
  const onSignal = () => {
    cleanup().finally(() => process.exit(130));
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  try {
    mkdirSync(join(opts.evidenceDir, "screenshots"), { recursive: true });
    const home = mkdtempSync(join(tmpdir(), "autonom-probe-harness-"));
    cleanups.push(() => rmSync(home, { recursive: true, force: true }));

    // Probe page server (the iframe origin) and host page server.
    let probeHtml = "";
    let csp = "default-src 'none';";
    const probeHandler = (req, res) => {
      const url = new URL(req.url, "http://localhost");
      if (req.method === "GET" && url.pathname === "/probe.html") {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": csp, "Cache-Control": "no-store" });
        res.end(probeHtml);
        return;
      }
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("not found");
    };
    const probeServer = http.createServer(probeHandler);
    const iframePort = await listen(probeServer, 0, "127.0.0.1");
    cleanups.push(() => closeServer(probeServer));
    const probeServer6 = http.createServer(probeHandler);
    try {
      await listen(probeServer6, iframePort, "::1");
      cleanups.push(() => closeServer(probeServer6));
    } catch {
      // No IPv6 loopback: localhost resolves to 127.0.0.1.
    }
    const iframeOrigin = `http://localhost:${iframePort}`;
    const probeUrl = `${iframeOrigin}/probe.html`;

    let bridge = null;
    const hostServer = http.createServer(async (req, res) => {
      const url = new URL(req.url, "http://127.0.0.1");
      if (req.method === "GET" && url.pathname === "/") {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
        res.end(hostPageHtml(probeUrl));
        return;
      }
      if (req.method === "POST" && url.pathname === "/bridge" && bridge) {
        let out = [];
        try {
          out = await bridge.handle(JSON.parse(await readBody(req)));
        } catch {
          out = [];
        }
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify(out));
        return;
      }
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("not found");
    });
    const hostPort = await listen(hostServer, 0, "127.0.0.1");
    cleanups.push(() => closeServer(hostServer));
    const hostUrl = `http://127.0.0.1:${hostPort}/`;

    // Media port and the remote route.
    const mediaPort = await freePort();
    let remoteRoute = null;
    let sshUnavailable = null;
    if (opts.remote.kind === "proxy") {
      const sockets = new Set();
      const proxy = net.createServer((client) => {
        const upstream = net.connect({ port: mediaPort, host: "127.0.0.1" });
        sockets.add(client);
        sockets.add(upstream);
        client.pipe(upstream);
        upstream.pipe(client);
        const drop = () => {
          client.destroy();
          upstream.destroy();
          sockets.delete(client);
          sockets.delete(upstream);
        };
        client.on("error", drop);
        upstream.on("error", drop);
        client.on("close", drop);
        upstream.on("close", drop);
      });
      const proxyPort = await listen(proxy, 0, "127.0.0.1");
      cleanups.push(async () => {
        for (const socket of sockets) socket.destroy();
        await closeServer(proxy);
      });
      remoteRoute = { label: PROXY_LABEL, origin: `http://127.0.0.1:${proxyPort}` };
      report.remote = { kind: "proxy", label: PROXY_LABEL, evidence_label: PROXY_KIND, origin: remoteRoute.origin };
    } else if (opts.remote.kind === "ssh") {
      const forwardPort = await freePort();
      const argv = sshArgv(opts.remote.host, forwardPort, mediaPort);
      const ssh = spawn(argv[0], argv.slice(1), { stdio: ["ignore", "ignore", "pipe"] });
      let sshErr = "";
      let sshExited = false;
      ssh.stderr.on("data", (chunk) => {
        sshErr = (sshErr + chunk.toString("utf8")).slice(-2000);
      });
      ssh.on("error", (error) => {
        sshExited = true;
        sshErr += String(error.message);
      });
      ssh.on("exit", () => {
        sshExited = true;
      });
      cleanups.push(async () => {
        if (!sshExited) {
          ssh.kill("SIGTERM");
          for (let i = 0; i < 20 && !sshExited; i += 1) await sleep(100);
          if (!sshExited) ssh.kill("SIGKILL");
        }
      });
      const deadline = Date.now() + SSH_WAIT_MS;
      let up = false;
      while (Date.now() < deadline && !sshExited) {
        if (await canConnect(forwardPort)) {
          up = true;
          break;
        }
        await sleep(200);
      }
      if (up) {
        remoteRoute = { label: SSH_LABEL, origin: `http://127.0.0.1:${forwardPort}` };
        report.remote = { kind: "ssh", label: SSH_LABEL, origin: remoteRoute.origin, argv };
      } else {
        if (!sshExited) ssh.kill("SIGTERM");
        sshUnavailable = `ssh forward unavailable: ${tail(sshErr.trim() || (sshExited ? "ssh exited" : "not listening within 10 s"), 200)}`;
        report.remote = { kind: "ssh", label: null, origin: null, argv, unavailable: sshUnavailable };
      }
    }

    // Config and the real server.
    const stateDir = join(home, "chatgpt-probe");
    mkdirSync(stateDir, { recursive: true, mode: 0o700 });
    const config = probeConfig({ mediaPort, iframeOrigin, remoteRoute, devices: opts.devices, node: process.execPath });
    writeFileSync(join(stateDir, "config.json"), `${JSON.stringify(config, null, 2)}\n`, { mode: 0o600 });
    const env = {};
    for (const [key, value] of Object.entries(process.env)) {
      if (!key.startsWith("AUTONOM_") && key !== "XDG_STATE_HOME") env[key] = value;
    }
    Object.assign(env, { AUTONOM_HOME: home, AUTONOM_CLI: AUTONOM_CLI, AUTONOM_ROOT: ROOT, AUTONOM_PROBE_NODE: process.execPath, PYTHONDONTWRITEBYTECODE: "1" });
    const proc = spawn("sh", [LAUNCHER], { cwd: PLUGIN, env, stdio: ["pipe", "pipe", "pipe"] });
    const mcp = new McpClient(proc);
    cleanups.push(() => mcp.stop());

    const init = await mcp.request("initialize", {
      protocolVersion: MCP_PROTOCOL,
      capabilities: { extensions: { "io.modelcontextprotocol/ui": { mimeTypes: [UI_MIME] } } },
      clientInfo: { ...HOST_INFO },
    });
    if (!init.result) throw new Error(`initialize failed: ${JSON.stringify(init.error)}; stderr: ${tail(mcp.stderr, 400)}`);
    mcp.notify("notifications/initialized", {});
    const tools = await mcp.request("tools/list", {});
    const resource = await mcp.request("resources/read", { uri: UI_URI });
    const content = resource.result && resource.result.contents && resource.result.contents[0];
    if (!content || typeof content.text !== "string") throw new Error(`resources/read failed: ${JSON.stringify(resource.error)}`);
    probeHtml = content.text;
    const uiMeta = (content._meta && content._meta.ui) || {};
    csp = buildCsp(uiMeta.csp || {});
    report.server = {
      protocol_version: init.result.protocolVersion, server_info: init.result.serverInfo,
      tools: (tools.result && tools.result.tools || []).map((t) => t.name), mime_type: content.mimeType, csp,
    };

    for (const browserName of opts.browsers) {
      report.browsers[browserName] = await runBrowser({
        browserName, core, opts, mcp, playwrightRoot, hostUrl, probeUrl, csp, remoteRoute, sshUnavailable, secrets,
        mediaPort, setBridge: (value) => { bridge = value; }, report,
      });
    }

    // Judge: Chromium with the harness profile, WebKit with the base check only.
    let ok = true;
    for (const browserName of opts.browsers) {
      const entry = report.browsers[browserName];
      const file = join(opts.evidenceDir, `results-${browserName}.json`);
      if (!existsSync(file)) {
        ok = false;
        continue;
      }
      const args = browserName === "chromium"
        ? ["--profile", "harness", "--results", `harness-chromium=${file}`]
        : ["--results", `harness-${browserName}=${file}`];
      entry.check = runChecker(args);
      if (entry.check.exit_code !== 0) ok = false;
    }
    report.ok = ok;
  } catch (error) {
    report.ok = false;
    report.error = tail(error && error.message ? error.message : String(error), 600, secrets);
  } finally {
    await cleanup();
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
  }
  process.stdout.write(`${JSON.stringify(scrubDeep(report, secrets, 2000), null, 2)}\n`);
  return report.ok ? 0 : 1;
}

async function runBrowser(ctx) {
  const { browserName, core, opts, mcp, playwrightRoot, hostUrl, probeUrl, csp, remoteRoute, sshUnavailable, secrets, mediaPort } = ctx;
  const dir = opts.evidenceDir;
  const resultsPath = join(dir, `results-${browserName}.json`);
  const entry = { results: resultsPath };
  const localOrigin = `http://127.0.0.1:${mediaPort}`;
  const notInstalled = `${browserName} not installed under PLAYWRIGHT_ROOT`;

  const playwright = await loadPlaywright(playwrightRoot).catch(() => null);
  const browserType = playwright ? playwright[browserName] : null;
  let executable = null;
  try {
    executable = browserType ? browserType.executablePath() : null;
  } catch {
    executable = null;
  }
  if (!browserType || !executable || !existsSync(executable)) {
    const stub = stubResults(core, notInstalled, { localOrigin });
    writeJson(resultsPath, stub);
    entry.stub = notInstalled;
    return entry;
  }

  // The host "opens" the probe: the model-facing open_workspace call whose result goes to the View.
  const open = await mcp.request("tools/call", { name: "open_workspace", arguments: {} }, 60_000);
  const openResult = open.result || {};
  const probeMeta = (openResult._meta && openResult._meta["autonom/probe"]) || {};
  if (typeof probeMeta.media_token === "string" && probeMeta.media_token) secrets.push(probeMeta.media_token);
  const nonce = typeof probeMeta.context_nonce === "string" ? probeMeta.context_nonce : null;
  entry.media_state = openResult.structuredContent && openResult.structuredContent.media ? openResult.structuredContent.media.state : null;

  const log = [];
  const started = Date.now();
  const bridge = new HostBridge({
    callTool: async (params) => {
      const answer = await mcp.request("tools/call", params, 60_000);
      return answer.error ? { error: answer.error } : { result: answer.result };
    },
    openResult,
    context: hostContext(VIEWPORTS.narrow),
    log: (direction, message) => log.push({ t_ms: Date.now() - started, direction, message: scrubDeep(message, secrets) }),
  });
  ctx.setBridge(bridge);

  const consoleErrors = [];
  const browser = await browserType.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: VIEWPORTS.narrow, colorScheme: "light" });
    const page = await context.newPage();
    page.on("console", (msg) => {
      if (msg.type() === "error" && consoleErrors.length < 100) consoleErrors.push(tail(msg.text(), 300, secrets));
    });
    page.on("pageerror", (error) => {
      if (consoleErrors.length < 100) consoleErrors.push(tail(String(error && error.message), 300, secrets));
    });
    await page.goto(hostUrl);
    let frame = null;
    for (let i = 0; i < 100 && !frame; i += 1) {
      frame = page.frames().find((f) => f.url().startsWith(probeUrl)) || null;
      if (!frame) await sleep(100);
    }
    if (!frame) throw new Error("the probe iframe did not load");
    const state = () => frame.evaluate(() => (globalThis.AutonomProbe ? globalThis.AutonomProbe.state : document.documentElement.dataset.probeState || null));
    const waitState = async (wanted, capMs) => {
      const deadline = Date.now() + capMs;
      while (Date.now() < deadline) {
        const now = await state().catch(() => null);
        if (now === wanted) return true;
        if (now === "broken") return false;
        await sleep(250);
      }
      return false;
    };
    entry.booted = await waitState("ready", 30_000);

    // Run all, as the operator would: a real click on the button.
    await frame.evaluate(() => { document.documentElement.dataset.probeState = "harness-wait"; });
    await frame.locator("#pb-run").click();
    entry.run_all_finished = await waitState("done", RUN_ALL_CAP_MS);

    // Operator for bridge.context: Passed only when the recorded model context carries the nonce.
    const contextRecord = async () => (await frame.evaluate(() => globalThis.AutonomProbe.results())).checks.find((c) => c.id === "bridge.context");
    const before = await contextRecord();
    if (before && before.status === "not_exercised" && /operator/i.test(before.reason || "")) {
      const answer = bridge.contextHasNonce(nonce) ? "Passed" : "Failed";
      await frame.locator('[data-check="bridge.context"] .pb-manual button', { hasText: answer }).click();
      for (let i = 0; i < 40; i += 1) {
        const now = await contextRecord();
        if (now && now.status !== "not_exercised") break;
        await sleep(250);
      }
      entry.operator_context = answer;
    }

    if (opts.devices.length) {
      await frame.evaluate(() => { document.documentElement.dataset.probeState = "harness-wait"; });
      await frame.locator("#pb-run-devices").click();
      entry.devices_finished = await waitState("done", DEVICE_CAP_MS);
    }

    await page.screenshot({ path: join(dir, "screenshots", `${browserName}-narrow.png`) });
    await page.setViewportSize(VIEWPORTS.wide);
    await page.evaluate((message) => window.__hostPost(message), {
      jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { containerDimensions: { ...VIEWPORTS.wide } },
    });
    log.push({ t_ms: Date.now() - started, direction: "host->view", message: { jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { containerDimensions: { ...VIEWPORTS.wide } } } });
    await sleep(1000);
    await page.screenshot({ path: join(dir, "screenshots", `${browserName}-wide.png`) });

    let results = await frame.evaluate(() => globalThis.AutonomProbe.results());
    if (sshUnavailable) results = annotateSshUnavailable(results, sshUnavailable);
    if (containsSecret(results, secrets)) {
      results = scrubDeep(results, secrets, Number.MAX_SAFE_INTEGER);
      entry.scrubbed_results = true;
    }
    writeJson(resultsPath, results);
    entry.summary = results.summary;

    // End of the run: the host tears the resource down.
    await page.evaluate((message) => window.__hostPost(message), { jsonrpc: "2.0", id: "teardown-1", method: "ui/resource-teardown", params: { reason: "harness run finished" } });
    log.push({ t_ms: Date.now() - started, direction: "host->view", message: { jsonrpc: "2.0", id: "teardown-1", method: "ui/resource-teardown" } });
    await sleep(500);
    await context.close();
  } catch (error) {
    entry.error = tail(error && error.message ? error.message : String(error), 600, secrets);
    if (!existsSync(resultsPath)) {
      writeJson(resultsPath, stubResults(core, `harness error in ${browserName}: ${tail(entry.error, 200)}`, { localOrigin }));
      entry.stub = "harness error";
    }
  } finally {
    await browser.close().catch(() => {});
    ctx.setBridge(null);
  }

  writeJson(join(dir, `host-log-${browserName}.json`), scrubDeep({
    browser: browserName,
    host_url: hostUrl,
    probe_url: probeUrl,
    csp,
    routes: { local: { origin: localOrigin }, remote: remoteRoute ? { ...remoteRoute, kind: opts.remote.kind === "proxy" ? PROXY_KIND : "ssh forward" } : null, ssh_unavailable: sshUnavailable },
    nonce_in_model_context: bridge.contextHasNonce(nonce),
    model_context: bridge.modelContexts,
    chat_messages: bridge.chatMessages,
    view_answers: bridge.viewAnswers,
    console_errors: consoleErrors,
    messages: log,
  }, secrets));
  return entry;
}

// ------------------------------------------------------------------------------------ entry

async function main(argv) {
  let opts;
  try {
    opts = parseCli(argv);
  } catch (error) {
    if (!(error instanceof UsageError)) throw error;
    const hint = error.code === "device_not_allowed"
      ? `Use only ${ALLOWED_DEVICES.join(" or ")}.`
      : USAGE;
    process.stderr.write(`${JSON.stringify({ ok: false, error_code: error.code, error: error.message, hint })}\n`);
    return 2;
  }
  if (opts.mode === "help") {
    process.stdout.write(`${USAGE}\n`);
    return 0;
  }
  if (opts.mode === "self-test") return await selfTest();
  if (opts.mode === "dry-run") {
    process.stdout.write(`${JSON.stringify(planFor(opts), null, 2)}\n`);
    return 0;
  }
  return await liveRun(opts);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  }, (error) => {
    process.stderr.write(`${JSON.stringify({ ok: false, error_code: "harness_failed", error: String(error && error.message ? error.message : error).slice(0, 400), hint: "Run with --dry-run to see the plan, then fix the named problem." })}\n`);
    process.exitCode = 1;
  });
}
