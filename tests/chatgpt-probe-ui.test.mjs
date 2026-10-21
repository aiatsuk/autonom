// Unit tests of the ChatGPT probe page (contract run ws-p0-host-contract, sections 5 and 8 "ui"):
// the pure logic in probe-core.mjs (checks, dependency blocking, results JSON, bridge client, frame
// header, numbers, origins) and static properties of probe-page.html (markers, no external resources,
// the 27 ids, accessibility hooks, tokens equal to the Canvas workspaceStyles()). No browser, host,
// server or device is involved; the only file written is a syntax-check copy in a mkdtemp directory.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test, { describe } from "node:test";
import { fileURLToPath } from "node:url";

import * as core from "../integrations/chatgpt/autonom-chatgpt-probe/ui/probe-core.mjs";
import { workspaceStyles } from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-workspace-page.mjs";

const {
  BridgeClient, BridgeError, CHECKS, CHECK_IDS, OPEN_WORKSPACE_FIELDS, PROBE_READ_FIELDS, REASONS, RUN_ALL_ORDER,
  blockDependants, containerSize, contextParams, createRun, cspCountFor, dependantsOf, detectEntry, executionOrder,
  finishRun, firstAllowedDevice, formatValue, initializeParams, isOpenWorkspaceResult, makeRunId, median, messageParams,
  missingFields, originOf, p95, parseFrameHeader, readOpenWorkspace, redact, remoteChoices, resetChecks, resultsJson,
  rowAccessibleName, scrubForExport, setStatus, summarize, summaryParts, toolOutcome, unmetDependency, wsOriginFor,
  wsProtocols, wsUrl, pingUrl,
} = core;

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const UI_DIR = join(ROOT, "integrations/chatgpt/autonom-chatgpt-probe/ui");
const PAGE = readFileSync(join(UI_DIR, "probe-page.html"), "utf8");
const CORE_SOURCE = readFileSync(join(UI_DIR, "probe-core.mjs"), "utf8");
const TOKEN = "tok_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_abcd";

const EXPECTED_IDS = [
  "net.fetch.local", "net.fetch.remote", "net.ws.local", "net.ws.remote", "input.rtt.local", "input.rtt.remote",
  "media.decode.local", "media.decode.remote", "net.reconnect.local", "net.reconnect.remote", "net.csp.local", "net.csp.remote",
  "host.init", "host.entry", "host.theme", "host.size", "bridge.read", "bridge.write", "bridge.error", "bridge.stale",
  "bridge.context", "bridge.message", "bridge.media", "model.tool", "media.webcodecs",
  "device.android.local", "device.ios.remote",
];

function fixedRun(startMs = Date.UTC(2026, 9, 10, 14, 22, 33)) {
  let t = startMs;
  const run = createRun({ now: () => t, randomHex: () => "7f3a" });
  run.advance = (ms) => { t += ms; };
  return run;
}

function hostSample() {
  return {
    name: "ChatGPT", version: "26.930.31730", protocol: "2026-01-26", entry: "global", display_mode: "fullscreen",
    theme: "dark", container: { width: 432, height: 780 }, platform: "desktop", user_agent: "x".repeat(300),
    token_path: "tool-result",
    mcp: { protocol_version: "2025-11-25", client: { name: "c", version: "1" }, client_ui_extension: true, methods_seen: ["initialize"] },
  };
}

function routesSample() {
  return {
    local: { origin: "http://127.0.0.1:47811/probe/ping?nonce=x" },
    remote: { origin: "http://user:pw@127.0.0.1:47812/path", label: "forwarded", selected: true },
  };
}

// Rules of contract 5.6, applied to a results object.
function assertResultsValid(results) {
  assert.equal(results.schema_version, 1);
  assert.equal(results.probe_version, "0.1.0");
  assert.match(results.run_id, /^probe-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$/);
  assert.match(results.started_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.match(results.finished_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.deepEqual(results.checks.map((c) => c.id), EXPECTED_IDS);
  const counts = { pass: 0, fail: 0, not_exercised: 0 };
  for (const check of results.checks) {
    assert.ok(["pass", "fail", "not_exercised"].includes(check.status), `${check.id} status ${check.status}`);
    assert.ok(["probe", "operator"].includes(check.observed_by), `${check.id} observed_by`);
    for (const key of ["id", "group", "route", "status", "observed_by", "value", "unit", "error", "reason", "depends_on", "started_at", "duration_ms"]) {
      assert.ok(Object.prototype.hasOwnProperty.call(check, key), `${check.id} has ${key}`);
    }
    if (check.status === "not_exercised") assert.ok(typeof check.reason === "string" && check.reason.length > 0, `${check.id} reason`);
    if (check.status === "fail") assert.match(check.error.code, /^[a-z][a-z0-9_]*$/, `${check.id} error.code`);
    if (check.status !== "fail") assert.equal(check.error, null);
    counts[check.status] += 1;
  }
  assert.deepEqual(results.summary, counts);
  for (const route of ["local", "remote"]) {
    const origin = results.routes[route].origin;
    if (origin !== null) assert.match(origin, /^[a-z]+:\/\/[^/?#@]+$/, `${route} origin is bare`);
  }
  const text = JSON.stringify(results);
  assert.ok(!text.includes(TOKEN), "token absent");
  assert.ok(!/bearer\./i.test(text), "no bearer. substring");
}

describe("CHECKS", () => {
  test("has the 27 ids of 5.3 in display order", () => {
    assert.equal(CHECKS.length, 27);
    assert.deepEqual([...CHECK_IDS], EXPECTED_IDS);
    assert.equal(new Set(CHECK_IDS).size, 27);
  });

  test("states the dependencies of 5.3 and 5.6", () => {
    const deps = Object.fromEntries(CHECKS.map((c) => [c.id, [...c.depends_on]]));
    for (const route of ["local", "remote"]) {
      assert.deepEqual(deps[`net.fetch.${route}`], []);
      assert.deepEqual(deps[`net.ws.${route}`], [`net.fetch.${route}`]);
      assert.deepEqual(deps[`input.rtt.${route}`], [`net.ws.${route}`]);
      assert.deepEqual(deps[`media.decode.${route}`], [`net.ws.${route}`, "media.webcodecs"]);
      assert.deepEqual(deps[`net.reconnect.${route}`], [`net.ws.${route}`, "media.webcodecs"]);
      assert.deepEqual(deps[`net.csp.${route}`], []);
    }
    assert.deepEqual(deps["bridge.media"], ["media.webcodecs"]);
    assert.deepEqual(deps["media.webcodecs"], []);
    assert.deepEqual(deps["device.android.local"], ["net.ws.local", "media.webcodecs"]);
    assert.deepEqual(deps["device.ios.remote"], ["net.ws.remote", "media.webcodecs"]);
    for (const check of CHECKS) for (const dep of check.depends_on) assert.ok(CHECK_IDS.includes(dep), `${check.id} -> ${dep}`);
  });

  test("descriptors carry group, route, manual flag and one describe sentence; they are frozen", () => {
    for (const check of CHECKS) {
      assert.ok(["routes", "host", "devices"].includes(check.group));
      assert.ok(check.route === null || check.route === "local" || check.route === "remote");
      assert.equal(typeof check.label, "string");
      assert.equal(typeof check.manual, "boolean");
      assert.match(check.describe, /^[A-Z].*\.$/);
      assert.ok(Object.isFrozen(check) && Object.isFrozen(check.depends_on));
    }
    assert.deepEqual(CHECKS.filter((c) => c.manual).map((c) => c.id), ["host.entry", "bridge.context", "model.tool"]);
    assert.ok(Object.isFrozen(CHECKS));
    assert.throws(() => { CHECKS.push({}); }, TypeError);
  });

  test("Run all order: host and bridge, then local, then remote; dependencies first; no devices", () => {
    assert.equal(RUN_ALL_ORDER.length, 25);
    assert.ok(!RUN_ALL_ORDER.some((id) => id.startsWith("device.")));
    const order = executionOrder(RUN_ALL_ORDER);
    assert.equal(order.length, 25);
    assert.ok(order.indexOf("media.webcodecs") < order.indexOf("bridge.media"));
    assert.ok(order.indexOf("model.tool") < order.indexOf("net.fetch.local"));
    assert.ok(order.indexOf("net.csp.local") < order.indexOf("net.fetch.remote"));
    for (const id of order) for (const dep of core.checkById(id).depends_on) assert.ok(order.indexOf(dep) < order.indexOf(id), `${dep} before ${id}`);
  });

  test("executionOrder pulls in dependencies only when include() allows it, and ignores unknown ids", () => {
    assert.deepEqual(executionOrder(["device.android.local"]), ["device.android.local"]);
    assert.deepEqual(executionOrder(["device.android.local", "nope"], { include: () => true }),
      ["net.fetch.local", "net.ws.local", "media.webcodecs", "device.android.local"]);
  });

  test("dependantsOf is direct by default and transitive on request", () => {
    assert.deepEqual(dependantsOf("net.fetch.local"), ["net.ws.local"]);
    assert.deepEqual(dependantsOf("net.fetch.local", { transitive: true }),
      ["input.rtt.local", "media.decode.local", "net.reconnect.local", "net.ws.local", "device.android.local"].sort((a, b) => CHECK_IDS.indexOf(a) - CHECK_IDS.indexOf(b)));
    assert.deepEqual(dependantsOf("media.webcodecs"),
      ["media.decode.local", "media.decode.remote", "net.reconnect.local", "net.reconnect.remote", "bridge.media", "device.android.local", "device.ios.remote"]);
  });
});

describe("run state", () => {
  test("createRun makes a UTC run_id and starts every check pending with its default reason", () => {
    const run = fixedRun();
    assert.equal(run.run_id, "probe-20261010-142233-7f3a");
    assert.equal(run.started_at, "2026-10-10T14:22:33Z");
    assert.equal(run.finished_at, null);
    for (const id of CHECK_IDS) {
      assert.equal(run.checks[id].status, "pending");
      assert.equal(run.checks[id].reason, id.startsWith("device.") ? REASONS.devices : REASONS.notStarted);
    }
  });

  test("makeRunId falls back to fresh hex when randomHex is bad or throws", () => {
    assert.match(makeRunId(0, () => "XYZ!"), /^probe-19700101-000000-[0-9a-f]{4}$/);
    assert.match(makeRunId(0, () => { throw new Error("no"); }), /^probe-19700101-000000-[0-9a-f]{4}$/);
    assert.match(createRun().run_id, core.RUN_ID_PATTERN);
  });

  test("setStatus refuses unknown ids and statuses", () => {
    const run = fixedRun();
    assert.throws(() => setStatus(run, "net.nope", { status: "pass" }), /Unknown check id/);
    assert.throws(() => setStatus(run, "net.fetch.local", { status: "pending" }), /Invalid status/);
    assert.throws(() => setStatus(run, "net.fetch.local", { status: "PASS" }), /Invalid status/);
    assert.throws(() => setStatus(run, "net.fetch.local", {}), /Invalid status/);
  });

  test("running starts the clock and a final status records duration", () => {
    const run = fixedRun();
    setStatus(run, "net.fetch.local", { status: "running" });
    assert.equal(run.checks["net.fetch.local"].started_at, "2026-10-10T14:22:33Z");
    run.advance(1234);
    const record = setStatus(run, "net.fetch.local", { status: "pass", value: 4, unit: "ms" });
    assert.equal(record.duration_ms, 1234);
    assert.equal(record.error, null);
    assert.equal(record.reason, null);
    assert.equal(record.observed_by, "probe");
  });

  test("fail always has an error code and not_exercised always has a reason", () => {
    const run = fixedRun();
    assert.deepEqual(setStatus(run, "net.ws.local", { status: "fail" }).error, { code: "unexpected_result", message: "unexpected_result", clue: null });
    assert.equal(setStatus(run, "net.ws.local", { status: "fail", error: { code: "WS_BAD code", message: "m" } }).error.code, "unexpected_result");
    assert.deepEqual(setStatus(run, "net.ws.local", { status: "fail", error: { code: "ws_closed_before_open", message: "closed 1006 before open", clue: "csp:connect-src" } }).error,
      { code: "ws_closed_before_open", message: "closed 1006 before open", clue: "csp:connect-src" });
    assert.equal(setStatus(run, "net.ws.local", { status: "fail", error: "plain text" }).error.message, "plain text");
    assert.equal(setStatus(run, "net.ws.local", { status: "not_exercised" }).reason, "Not exercised");
    assert.equal(setStatus(run, "net.ws.local", { status: "not_exercised", reason: "   " }).reason, "Not exercised");
    const operator = setStatus(run, "bridge.context", { status: "pass", observed_by: "operator", value: "probe-7f3a-lime" });
    assert.equal(operator.observed_by, "operator");
    assert.equal(setStatus(run, "bridge.context", { status: "pass", observed_by: "robot" }).observed_by, "probe");
  });

  test("blockDependants marks pending dependants not_exercised with Needs <id>, transitively, never fail", () => {
    const run = fixedRun();
    setStatus(run, "input.rtt.local", { status: "pass", value: { median: 1, p95: 2, n: 20 }, unit: "ms" });
    setStatus(run, "net.fetch.local", { status: "fail", error: { code: "fetch_failed", message: "x" } });
    const changed = blockDependants(run, "net.fetch.local");
    assert.ok(changed.includes("net.ws.local"));
    assert.equal(run.checks["net.ws.local"].status, "not_exercised");
    assert.equal(run.checks["net.ws.local"].reason, "Needs net.fetch.local");
    assert.equal(run.checks["media.decode.local"].reason, "Needs net.ws.local");
    assert.equal(run.checks["net.reconnect.local"].reason, "Needs net.ws.local");
    assert.equal(run.checks["device.android.local"].reason, "Needs net.ws.local");
    assert.equal(run.checks["input.rtt.local"].status, "pass", "a check that already ran keeps its result");
    for (const id of CHECK_IDS) assert.notEqual(run.checks[id].status === "fail" && id !== "net.fetch.local", true);
    assert.equal(run.checks["net.ws.remote"].status, "pending");
  });

  test("blockDependants does nothing for a pass, a running check or an unknown id", () => {
    const run = fixedRun();
    setStatus(run, "net.fetch.local", { status: "pass", value: 3, unit: "ms" });
    assert.deepEqual(blockDependants(run, "net.fetch.local"), []);
    setStatus(run, "media.webcodecs", { status: "running" });
    assert.deepEqual(blockDependants(run, "media.webcodecs"), []);
    assert.deepEqual(blockDependants(run, "missing.id"), []);
    setStatus(run, "media.webcodecs", { status: "not_exercised", reason: "Not running inside an MCP host" });
    assert.ok(blockDependants(run, "media.webcodecs").includes("bridge.media"));
    assert.equal(run.checks["bridge.media"].reason, "Needs media.webcodecs");
  });

  test("unmetDependency names the first dependency that did not pass", () => {
    const run = fixedRun();
    assert.equal(unmetDependency(run, "media.decode.local"), "net.ws.local");
    setStatus(run, "net.ws.local", { status: "pass" });
    assert.equal(unmetDependency(run, "media.decode.local"), "media.webcodecs");
    setStatus(run, "media.webcodecs", { status: "pass", value: ["avc1.42C01E"] });
    assert.equal(unmetDependency(run, "media.decode.local"), null);
    assert.equal(unmetDependency(run, "nope"), null);
  });

  test("resetChecks puts checks back to pending", () => {
    const run = fixedRun();
    setStatus(run, "net.fetch.local", { status: "fail", error: { code: "fetch_failed" } });
    resetChecks(run, ["net.fetch.local", "unknown.id"]);
    assert.equal(run.checks["net.fetch.local"].status, "pending");
    assert.equal(run.checks["net.fetch.local"].error, null);
  });

  test("summarize counts pending and running as not exercised", () => {
    const run = fixedRun();
    setStatus(run, "net.fetch.local", { status: "pass", value: 1, unit: "ms" });
    setStatus(run, "net.ws.local", { status: "fail", error: { code: "ws_echo_mismatch" } });
    setStatus(run, "input.rtt.local", { status: "running" });
    assert.deepEqual(summarize(run), { pass: 1, fail: 1, not_exercised: 25 });
  });
});

describe("resultsJson", () => {
  function completedRun() {
    const run = fixedRun();
    for (const id of executionOrder(RUN_ALL_ORDER)) {
      if (run.checks[id].status !== "pending") continue;
      setStatus(run, id, { status: "running" });
      run.advance(10);
      if (id === "net.ws.remote") {
        setStatus(run, id, { status: "fail", error: { code: "ws_closed_before_open", message: `closed 1006 before open bearer.${TOKEN}`, clue: "csp:connect-src" } });
      } else if (id === "bridge.context") {
        setStatus(run, id, { status: "not_exercised", reason: REASONS.contextOperator, value: "probe-7f3a-lime" });
      } else {
        setStatus(run, id, { status: "pass", value: 5, unit: "ms" });
      }
      blockDependants(run, id);
    }
    run.advance(31000);
    finishRun(run);
    return run;
  }

  test("produces the 5.6 object and satisfies every rule after scrubbing", () => {
    const run = completedRun();
    const raw = resultsJson(run, { host: hostSample(), routes: routesSample() });
    const results = scrubForExport(raw, [TOKEN]);
    assertResultsValid(results);
    assert.equal(results.finished_at, "2026-10-10T14:23:04Z");
    assert.deepEqual(results.routes, {
      local: { origin: "http://127.0.0.1:47811" },
      remote: { origin: "http://127.0.0.1:47812", label: "forwarded", selected: true },
    });
    assert.equal(results.host.user_agent.length, 200);
    assert.equal(results.host.mcp.protocol_version, "2025-11-25");
    const ws = results.checks.find((c) => c.id === "net.ws.remote");
    assert.equal(ws.group, "routes");
    assert.equal(ws.route, "remote");
    assert.deepEqual(ws.depends_on, ["net.fetch.remote"]);
    assert.equal(ws.error.clue, "csp:connect-src");
    assert.ok(ws.error.message.startsWith("closed 1006 before open"));
    const context = results.checks.find((c) => c.id === "bridge.context");
    assert.equal(context.value, "probe-7f3a-lime");
    assert.equal(results.checks.find((c) => c.id === "input.rtt.remote").reason, "Needs net.ws.remote");
    assert.equal(results.checks.find((c) => c.id === "device.android.local").reason, REASONS.devices);
    assert.deepEqual(results.summary, { pass: 20, fail: 1, not_exercised: 6 });
  });

  test("a fresh run is valid: every check not_exercised with a reason", () => {
    const results = resultsJson(fixedRun(), {});
    assertResultsValid(results);
    assert.deepEqual(results.summary, { pass: 0, fail: 0, not_exercised: 27 });
    assert.deepEqual(results.routes, { local: { origin: null }, remote: { origin: null, label: null, selected: false } });
    assert.equal(results.host.entry, "unknown");
    assert.equal(results.host.mcp, null);
  });

  test("a check still running is reported as interrupted, never as running", () => {
    const run = fixedRun();
    setStatus(run, "media.decode.local", { status: "running" });
    const check = resultsJson(run).checks.find((c) => c.id === "media.decode.local");
    assert.equal(check.status, "not_exercised");
    assert.equal(check.reason, REASONS.interrupted);
    assert.equal(check.duration_ms, null);
  });

  test("remote selected without a valid origin is not selected; bad host fields are dropped", () => {
    const results = resultsJson(fixedRun(), {
      routes: { local: { origin: "file:///x" }, remote: { origin: "nonsense", label: "x", selected: true } },
      host: { entry: "elsewhere", theme: "sepia", container: { width: "432", height: null }, media_token: TOKEN },
    });
    assert.deepEqual(results.routes, { local: { origin: null }, remote: { origin: null, label: "x", selected: false } });
    assert.equal(results.host.entry, "unknown");
    assert.equal(results.host.theme, null);
    assert.deepEqual(results.host.container, { width: 432, height: null });
    assert.ok(!JSON.stringify(results).includes(TOKEN), "unknown host keys are not copied");
  });

  test("the five fields the doc matrix reads survive a JSON round trip", () => {
    const results = JSON.parse(JSON.stringify(scrubForExport(resultsJson(completedRun(), { host: hostSample(), routes: routesSample() }), [TOKEN])));
    for (const check of results.checks) {
      assert.equal(typeof check.id, "string");
      assert.equal(typeof check.status, "string");
      assert.ok("value" in check && "reason" in check && "error" in check);
    }
  });
});

describe("redaction", () => {
  test("redact replaces the token, bearer.<token> and Bearer <token>", () => {
    assert.equal(redact(`a ${TOKEN} b`, [TOKEN]), "a [redacted] b");
    assert.equal(redact(`proto bearer.${TOKEN}`, [TOKEN]), "proto [redacted]");
    assert.equal(redact(`Authorization: Bearer ${TOKEN}`, [TOKEN]), "Authorization: [redacted]");
    assert.equal(redact("nothing here", [TOKEN]), "nothing here");
    assert.equal(redact("short ab", ["ab", "", null]), "short ab", "secrets under 4 chars are ignored");
    assert.equal(redact(42, [TOKEN]), 42);
    assert.equal(redact(`x${TOKEN}y`, TOKEN), "x[redacted]y");
  });

  test("scrubForExport walks keys and values and neutralises any leftover bearer.", () => {
    const out = scrubForExport({ [`k${TOKEN}`]: [`v ${TOKEN}`, { deep: "Bearer.other" }], n: 1, b: true, z: null }, [TOKEN]);
    const text = JSON.stringify(out);
    assert.ok(!text.includes(TOKEN));
    assert.ok(!/bearer\./i.test(text));
    assert.equal(out.n, 1);
    assert.equal(out.b, true);
    assert.equal(out.z, null);
  });
});

describe("numbers and origins", () => {
  test("median and p95", () => {
    assert.equal(median([]), null);
    assert.equal(median([5]), 5);
    assert.equal(median([3, 1, 2]), 2);
    assert.equal(median([4, 1, 3, 2]), 2.5);
    assert.equal(median([1, "x", NaN, 3]), 2);
    assert.equal(p95([]), null);
    assert.equal(p95([7]), 7);
    assert.equal(p95(Array.from({ length: 20 }, (_, i) => i + 1)), 19);
    assert.equal(p95(Array.from({ length: 100 }, (_, i) => 100 - i)), 95);
  });

  test("originOf strips path, query, fragment and userinfo, lowercases and refuses other schemes", () => {
    assert.equal(originOf("http://user:pw@127.0.0.1:47811/probe/ping?nonce=x#f"), "http://127.0.0.1:47811");
    assert.equal(originOf("HTTPS://Probe.Example.TS.net/"), "https://probe.example.ts.net");
    assert.equal(originOf("https://h:443/x"), "https://h");
    assert.equal(originOf("ws://127.0.0.1:47811/probe/ws?source=fixture"), "ws://127.0.0.1:47811");
    assert.equal(originOf("wss://h:8443"), "wss://h:8443");
    for (const bad of ["file:///etc/passwd", "null", "", null, undefined, 42, "javascript:alert(1)", "data:text/plain,x", "not a url"]) {
      assert.equal(originOf(bad), null, String(bad));
    }
  });

  test("wsOriginFor and URL helpers", () => {
    assert.equal(wsOriginFor("http://127.0.0.1:47811/x"), "ws://127.0.0.1:47811");
    assert.equal(wsOriginFor("https://h.example:8443"), "wss://h.example:8443");
    assert.equal(wsOriginFor("ws://h:1"), "ws://h:1");
    assert.equal(wsOriginFor("ftp://h"), null);
    assert.equal(pingUrl("http://127.0.0.1:47811", "n a"), "http://127.0.0.1:47811/probe/ping?nonce=n%20a");
    assert.equal(wsUrl("http://127.0.0.1:47811", "android:emulator-5584"), "ws://127.0.0.1:47811/probe/ws?source=android%3Aemulator-5584");
    assert.deepEqual(wsProtocols("t"), ["autonom-probe.v1", "bearer.t"]);
  });

  test("cspCountFor counts violations for the route origin and its ws twin only", () => {
    const violations = [
      { blockedURI: "http://127.0.0.1:47812/probe/ping?nonce=a" },
      { blockedURI: "ws://127.0.0.1:47812/probe/ws?source=fixture" },
      { blockedURI: "http://127.0.0.1:47811/probe/ping" },
      { blockedURI: "inline" },
      null,
    ];
    assert.equal(cspCountFor(violations, "http://127.0.0.1:47812"), 2);
    assert.equal(cspCountFor(violations, "http://127.0.0.1:47811"), 1);
    assert.equal(cspCountFor(violations, "http://127.0.0.1:1"), 0);
    assert.equal(cspCountFor(violations, null), 0);
    assert.equal(cspCountFor("nope", "http://127.0.0.1:47811"), 0);
  });

  test("firstAllowedDevice picks the first device of a platform and never emulator-5554", () => {
    const allowed = ["android:emulator-5554", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", "android:emulator-5584", 7];
    assert.deepEqual(firstAllowedDevice(allowed, "android"), { spec: "android:emulator-5584", id: "emulator-5584" });
    assert.deepEqual(firstAllowedDevice(allowed, "ios"), { spec: "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", id: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5" });
    assert.equal(firstAllowedDevice(["android:emulator-5554"], "android"), null);
    assert.equal(firstAllowedDevice(null, "ios"), null);
  });

  test("remoteChoices keeps labelled valid origins only, deduplicated by label", () => {
    assert.deepEqual(remoteChoices([
      { label: "forwarded", origin: "http://127.0.0.1:47812/" },
      { label: "forwarded", origin: "http://127.0.0.1:47813" },
      { label: "", origin: "http://127.0.0.1:1" },
      { label: "bad", origin: "file:///x" },
      null,
    ]), [{ label: "forwarded", origin: "http://127.0.0.1:47812" }]);
    assert.deepEqual(remoteChoices(undefined), []);
  });
});

describe("parseFrameHeader", () => {
  function frame({ magic = 0xa7, flags = 3, seq = 12, t = 1760000000000.5, payload = [0, 0, 0, 1, 0x67] } = {}) {
    const buf = new ArrayBuffer(16 + payload.length);
    const view = new DataView(buf);
    view.setUint8(0, magic);
    view.setUint8(1, flags);
    view.setUint32(4, seq, false);
    view.setFloat64(8, t, false);
    new Uint8Array(buf, 16).set(payload);
    return buf;
  }

  test("reads flags, seq, server time and the payload", () => {
    const parsed = parseFrameHeader(frame());
    assert.equal(parsed.key, true);
    assert.equal(parsed.config, true);
    assert.equal(parsed.seq, 12);
    assert.equal(parsed.tServerMs, 1760000000000.5);
    assert.deepEqual([...parsed.payload], [0, 0, 0, 1, 0x67]);
    const delta = parseFrameHeader(frame({ flags: 0, seq: 4294967295 }));
    assert.equal(delta.key, false);
    assert.equal(delta.config, false);
    assert.equal(delta.seq, 4294967295);
  });

  test("accepts a view with an offset (Node Buffer from a pool)", () => {
    const bytes = new Uint8Array(frame({ flags: 1, seq: 7 }));
    const padded = new Uint8Array(bytes.length + 5);
    padded.set(bytes, 5);
    const parsed = parseFrameHeader(padded.subarray(5));
    assert.equal(parsed.seq, 7);
    assert.equal(parsed.key, true);
    assert.equal(parsed.config, false);
    assert.equal(parseFrameHeader(Buffer.from(bytes)).seq, 7);
  });

  test("throws on bad magic, short frames and non-binary input", () => {
    assert.throws(() => parseFrameHeader(frame({ magic: 0x00 })), /magic/);
    assert.throws(() => parseFrameHeader(new ArrayBuffer(16)), /too short/);
    assert.throws(() => parseFrameHeader(new ArrayBuffer(3)), /too short/);
    assert.throws(() => parseFrameHeader("text"), TypeError);
    assert.throws(() => parseFrameHeader(null), TypeError);
  });

  test("base64ToBytes decodes frame data", () => {
    assert.deepEqual([...core.base64ToBytes(Buffer.from([0, 0, 0, 1, 0x65, 255]).toString("base64"))], [0, 0, 0, 1, 0x65, 255]);
    assert.throws(() => core.base64ToBytes(null), TypeError);
  });
});

describe("tool results (5.7 fields)", () => {
  const openWorkspace = {
    content: [{ type: "text", text: "{}" }],
    structuredContent: {
      schema_version: "autonom.chatgpt-probe/v1", operation_id: "op_0123456789abcdef", ok: true, probe_version: "0.1.0",
      media: {
        state: "running", local_origin: "http://127.0.0.1:47811", port: 47811,
        remote_routes: [{ label: "forwarded", origin: "http://127.0.0.1:47812" }],
        allowed_devices: ["android:emulator-5584"], error_code: null, error: null, hint: null,
      },
      selection: { target_ref: null, revision: 0 },
      note: { revision: 3 },
    },
    isError: false,
    _meta: { "autonom/probe": { media_token: TOKEN, context_nonce: "probe-7f3a-lime" } },
  };
  const probeRead = {
    schema_version: "autonom.chatgpt-probe/v1", operation_id: "op_0123456789abcdef", ok: true,
    host: { hostname: "h", platform: "darwin", os_release: "27.0.0", machine: "arm64" },
    server: { probe_version: "0.1.0", python: "3.9.6", pid: 1, protocol_version: "2025-11-25", requested_protocol_version: "2025-11-25",
      client: { name: "c", version: "1" }, client_ui_extension: true, client_ui_mime_types: ["text/html;profile=mcp-app"],
      methods_seen: ["initialize"], config_error: null },
    autonom: { version: "0.35.1", cli: "autonom", cli_python: "3.14" },
    devices: [], devices_warnings: 0,
    media: { state: "running", port: 47811, clients: 0, refused_origins: [], error_code: null },
    note: { revision: 3, text: "hello", updated_at: "2026-10-10T14:22:33Z" },
    selection: { target_ref: null, revision: 2 },
  };

  test("the contract examples carry every field the page reads", () => {
    assert.deepEqual(missingFields(openWorkspace.structuredContent, OPEN_WORKSPACE_FIELDS), []);
    assert.deepEqual(missingFields(probeRead, PROBE_READ_FIELDS), []);
    assert.deepEqual(missingFields({ ok: true, server: {} }, ["ok", "server.client", "note.text"]), ["server.client", "note.text"]);
    assert.deepEqual(missingFields(null, ["ok"]), ["ok"]);
  });

  test("readOpenWorkspace keeps the 5.7 fields and the token only in memory", () => {
    assert.equal(isOpenWorkspaceResult(openWorkspace), true);
    const ws = readOpenWorkspace(openWorkspace);
    assert.equal(ws.ok, true);
    assert.equal(ws.probe_version, "0.1.0");
    assert.deepEqual(ws.media, {
      state: "running", local_origin: "http://127.0.0.1:47811", port: 47811,
      remote_routes: [{ label: "forwarded", origin: "http://127.0.0.1:47812" }], allowed_devices: ["android:emulator-5584"],
      error_code: null, error: null, hint: null,
    });
    assert.deepEqual(ws.selection, { target_ref: null, revision: 0 });
    assert.deepEqual(ws.note, { revision: 3 });
    assert.equal(ws.media_token, TOKEN);
    assert.equal(ws.context_nonce, "probe-7f3a-lime");
  });

  test("readOpenWorkspace of a media failure keeps error, hint and a null token", () => {
    const failed = structuredClone(openWorkspace);
    failed.structuredContent.media = { state: "failed", local_origin: "http://127.0.0.1:47811", port: 47811, remote_routes: [], allowed_devices: [],
      error_code: "media_port_in_use", error: "port busy", hint: "change media_port" };
    failed._meta["autonom/probe"].media_token = null;
    const ws = readOpenWorkspace(failed);
    assert.equal(ws.ok, true);
    assert.equal(ws.media.state, "failed");
    assert.equal(ws.media.error_code, "media_port_in_use");
    assert.equal(ws.media.hint, "change media_port");
    assert.equal(ws.media_token, null);
    assert.equal(isOpenWorkspaceResult(failed), true);
  });

  test("isOpenWorkspaceResult is false for other tools", () => {
    assert.equal(isOpenWorkspaceResult({ structuredContent: probeRead }), false);
    assert.equal(isOpenWorkspaceResult(null), false);
  });

  test("toolOutcome handles success, isError, ok:false and content-only results", () => {
    assert.equal(toolOutcome({ structuredContent: probeRead }).ok, true);
    const failure = toolOutcome({
      isError: true,
      structuredContent: { ok: false, error_code: "probe_deliberate_failure", error: "Deliberate failure for the bridge.error check", hint: "h" },
    });
    assert.equal(failure.ok, false);
    assert.equal(failure.isError, true);
    assert.equal(failure.errorCode, "probe_deliberate_failure");
    const stale = toolOutcome({ structuredContent: { ok: false, error_code: "stale_selection", current_selection_revision: 5 } });
    assert.equal(stale.isError, true);
    assert.equal(stale.sc.current_selection_revision, 5);
    const textOnly = toolOutcome({ content: [{ type: "text", text: JSON.stringify({ ok: true, frames: 2 }) }] });
    assert.equal(textOnly.ok, true);
    assert.equal(textOnly.sc.frames, 2);
    const garbage = toolOutcome({ content: [{ type: "text", text: "not json" }] });
    assert.equal(garbage.ok, false);
    assert.deepEqual(garbage.sc, {});
    assert.equal(toolOutcome(undefined).ok, false);
    assert.equal(toolOutcome({ isError: true, structuredContent: { ok: true } }).ok, false, "isError wins over ok:true");
    const frames = toolOutcome({ structuredContent: { ok: true, frames: 1 }, _meta: { "autonom/probe": { frames: [{ seq: 1, key: true, t_server_ms: 1, data_b64: "AA==" }] } } });
    assert.equal(frames.meta.frames[0].seq, 1);
  });
});

describe("bridge payloads and presentation", () => {
  test("initializeParams follows 5.4 step 2", () => {
    assert.deepEqual(initializeParams(), {
      appInfo: { name: "autonom-probe", version: "0.1.0" },
      appCapabilities: { availableDisplayModes: ["fullscreen", "inline"] },
      protocolVersion: "2026-01-26",
    });
  });

  test("contextParams carries the nonce and selection, never anything else", () => {
    const params = contextParams("probe-7f3a-lime", { target_ref: "fixture~probe-b", revision: 2 });
    assert.equal(params.content[0].type, "text");
    assert.match(params.content[0].text, /Probe nonce: probe-7f3a-lime\. Current selection: fixture~probe-b at selection_revision 2\./);
    assert.deepEqual(params.structuredContent, { autonom_probe: { nonce: "probe-7f3a-lime", selection: { target_ref: "fixture~probe-b", revision: 2 } } });
    assert.match(contextParams("n", {}).content[0].text, /Current selection: none at selection_revision 0/);
  });

  test("messageParams sends a summary, with the JSON fenced only when given", () => {
    assert.deepEqual(messageParams("short"), { role: "user", content: [{ type: "text", text: "short" }] });
    const full = messageParams("line", { a: 1 }).content[0].text;
    assert.ok(full.startsWith("line\n```json\n"));
    assert.ok(full.endsWith("\n```"));
    assert.deepEqual(JSON.parse(full.slice("line\n```json\n".length, -4)), { a: 1 });
  });

  test("detectEntry and containerSize", () => {
    assert.equal(detectEntry({}), "unknown");
    assert.equal(detectEntry(null), "unknown");
    assert.equal(detectEntry({ entrypoint: "global" }), "global");
    assert.equal(detectEntry({ entrypoint: { type: "thread" } }), "thread");
    assert.equal(detectEntry({ location: "sidebar" }), "global");
    assert.equal(detectEntry({ displayMode: "fullscreen" }), "unknown");
    assert.deepEqual(containerSize({ width: 432, height: 780 }), { width: 432, height: 780 });
    assert.deepEqual(containerSize({ maxWidth: 400, maxHeight: 600.4 }), { width: 400, height: 600 });
    assert.deepEqual(containerSize(undefined), { width: null, height: null });
  });

  test("formatValue, rowAccessibleName and summaryParts", () => {
    const run = fixedRun();
    setStatus(run, "net.ws.local", { status: "pass", value: 9, unit: "ms" });
    setStatus(run, "net.ws.remote", { status: "fail", error: { code: "ws_closed_before_open" } });
    assert.equal(rowAccessibleName("WebSocket", [{ route: "local", record: run.checks["net.ws.local"] }, { route: "remote", record: run.checks["net.ws.remote"] }]),
      "WebSocket: local pass 9 ms, remote fail");
    assert.equal(formatValue({ id: "input.rtt.local", value: { median: 18.2, p95: 41, n: 20 } }), "18/41 ms");
    assert.equal(formatValue({ id: "input.rtt.local", value: { median: 0.2, p95: 0.54, n: 20 } }), "0.2/0.5 ms");
    assert.equal(formatValue({ id: "net.fetch.local", value: 4.44, unit: "ms" }), "4.4 ms");
    assert.equal(formatValue({ id: "net.fetch.local", value: 61.6, unit: "ms" }), "62 ms");
    assert.equal(core.formatMs(null), "");
    assert.equal(core.formatMs("x"), "");
    assert.equal(formatValue({ id: "media.decode.local", value: { fps: 59.4, decode_ms_median: 2.1 } }), "59 fps");
    assert.equal(formatValue({ id: "host.init", value: { name: "ChatGPT", version: "26.930" } }), "ChatGPT 26.930");
    assert.equal(formatValue({ id: "host.entry", value: "global" }), "Sidebar");
    assert.equal(formatValue({ id: "net.csp.local", value: 0, unit: "count" }), "0");
    assert.equal(formatValue({ id: "device.android.local", value: { target: "emulator-5584" } }), "emulator-5584");
    assert.equal(formatValue({ id: "media.webcodecs", value: [] }), "none");
    assert.equal(formatValue({ id: "bridge.message", value: null }), "");
    const parts = summaryParts({ pass: 14, fail: 2, not_exercised: 11 });
    assert.equal(parts.map((p) => p.text).join(""), "14 passed · 2 failed · 11 not exercised");
    assert.deepEqual(parts.filter((p) => p.danger).map((p) => p.text), ["2 failed"]);
    assert.equal(summaryParts({ pass: 1, fail: 0, not_exercised: 0 }).some((p) => p.danger), false);
    assert.match(core.summaryLine(run), /^Autonom probe 0\.1\.0 run probe-20261010-142233-7f3a: 1 passed, 1 failed, 25 not exercised\.$/);
  });
});

describe("BridgeClient", () => {
  function harness({ timeoutMs = 50 } = {}) {
    const parent = { name: "parent" };
    const posted = [];
    let listener = null;
    const timers = [];
    const client = new BridgeClient({
      post: (message) => posted.push(message),
      addListener: (fn) => { listener = fn; },
      parent,
      timeoutMs,
      setTimer: (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; },
      clearTimer: (t) => { if (t) t.cleared = true; },
    });
    const deliver = (data, source = parent) => listener({ source, data });
    const fire = () => { for (const t of timers.splice(0)) if (!t.cleared) t.fn(); };
    return { client, parent, posted, deliver, fire, timers };
  }

  test("requests use increasing integer ids and resolve with the result", async () => {
    const { client, posted, deliver } = harness();
    const first = client.request("ui/initialize", { a: 1 });
    const second = client.request("tools/call", { name: "probe_read", arguments: {} });
    assert.deepEqual(posted, [
      { jsonrpc: "2.0", id: 1, method: "ui/initialize", params: { a: 1 } },
      { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "probe_read", arguments: {} } },
    ]);
    deliver({ jsonrpc: "2.0", id: 2, result: { ok: 2 } });
    deliver({ jsonrpc: "2.0", id: 1, result: { ok: 1 } });
    assert.deepEqual(await first, { ok: 1 });
    assert.deepEqual(await second, { ok: 2 });
    assert.equal(client.pendingCount, 0);
  });

  test("a request without params omits the key; a null result resolves null", async () => {
    const { client, posted, deliver } = harness();
    const p = client.request("ping");
    assert.deepEqual(posted[0], { jsonrpc: "2.0", id: 1, method: "ping" });
    deliver({ jsonrpc: "2.0", id: 1 });
    assert.equal(await p, null);
  });

  test("timeout rejects with bridge_timeout and a late answer is ignored", async () => {
    const { client, deliver, fire, timers } = harness({ timeoutMs: 8000 });
    const p = client.request("ui/initialize", {});
    assert.equal(timers[0].ms, 8000);
    fire();
    await assert.rejects(p, (error) => error instanceof BridgeError && error.code === "bridge_timeout" && /8000 ms/.test(error.message));
    assert.equal(deliver({ jsonrpc: "2.0", id: 1, result: {} }), false);
  });

  test("per-request timeout overrides the default", async () => {
    const { client, timers } = harness({ timeoutMs: 8000 });
    client.request("tools/call", {}, { timeoutMs: 45000 }).catch(() => {});
    assert.equal(timers[0].ms, 45000);
  });

  test("error responses reject with bridge_error and keep the JSON-RPC error", async () => {
    const { client, deliver } = harness();
    const p = client.request("tools/call", {});
    deliver({ jsonrpc: "2.0", id: 1, error: { code: -32602, message: "Unknown tool: x" } });
    await assert.rejects(p, (error) => error.code === "bridge_error" && error.data.code === -32602 && /Unknown tool: x/.test(error.message));
  });

  test("a post that throws rejects with bridge_error", async () => {
    const client = new BridgeClient({ post: () => { throw new Error("detached"); }, parent: {}, setTimer: () => 1, clearTimer: () => {} });
    await assert.rejects(client.request("x"), (error) => error.code === "bridge_error" && /detached/.test(error.message));
    assert.equal(client.pendingCount, 0);
  });

  test("close rejects pending requests and refuses new ones", async () => {
    const { client } = harness();
    const p = client.request("slow");
    client.close();
    await assert.rejects(p, (error) => error.code === "bridge_error" && /closed/.test(error.message));
    await assert.rejects(client.request("again"), (error) => error.code === "bridge_error");
    client.notify("ignored");
  });

  test("notify sends a message without id", () => {
    const { client, posted } = harness();
    client.notify("ui/notifications/initialized", {});
    client.notify("bare");
    assert.deepEqual(posted, [{ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }, { jsonrpc: "2.0", method: "bare" }]);
  });

  test("inbound ui/resource-teardown is answered with {}", async () => {
    const { posted, deliver } = harness();
    deliver({ jsonrpc: "2.0", id: "t1", method: "ui/resource-teardown", params: {} });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(posted, [{ jsonrpc: "2.0", id: "t1", result: {} }]);
  });

  test("custom request handlers, unknown methods (-32601) and throwing handlers (-32603)", async () => {
    const { client, posted, deliver } = harness();
    client.onRequest("ui/resource-teardown", async () => ({ done: true }));
    client.onRequest("boom", () => { throw new Error("bad handler"); });
    deliver({ jsonrpc: "2.0", id: 5, method: "ui/resource-teardown" });
    deliver({ jsonrpc: "2.0", id: 6, method: "nope/unknown" });
    deliver({ jsonrpc: "2.0", id: 7, method: "boom" });
    await new Promise((resolve) => setImmediate(resolve));
    posted.sort((a, b) => a.id - b.id);
    assert.deepEqual(posted, [
      { jsonrpc: "2.0", id: 5, result: { done: true } },
      { jsonrpc: "2.0", id: 6, error: { code: -32601, message: "Method not found", data: { method: "nope/unknown" } } },
      { jsonrpc: "2.0", id: 7, error: { code: -32603, message: "bad handler" } },
    ]);
  });

  test("notifications go to every handler; a throwing handler does not stop the others", () => {
    const { client, deliver } = harness();
    const seen = [];
    client.onNotification("ui/notifications/tool-result", () => { throw new Error("view bug"); });
    client.onNotification("ui/notifications/tool-result", (params) => seen.push(params));
    client.onNotification("ui/notifications/host-context-changed", (params) => seen.push(params));
    assert.equal(deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: { ok: true } } }), true);
    deliver({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed" });
    deliver({ jsonrpc: "2.0", method: "unhandled/notification", params: {} });
    assert.deepEqual(seen, [{ structuredContent: { ok: true } }, {}]);
  });

  test("messages from other sources, without jsonrpc 2.0 or malformed are ignored", async () => {
    const { client, deliver } = harness();
    const seen = [];
    client.onNotification("ui/notifications/tool-result", (params) => seen.push(params));
    const p = client.request("ui/initialize", {});
    assert.equal(deliver({ jsonrpc: "2.0", id: 1, result: { forged: true } }, { name: "other frame" }), false);
    assert.equal(deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: {} }, null), false);
    assert.equal(deliver({ jsonrpc: "1.0", id: 1, result: {} }), false);
    assert.equal(deliver("not an object"), false);
    assert.equal(deliver(null), false);
    assert.equal(deliver({ jsonrpc: "2.0", id: 99, result: {} }), false, "unknown id");
    assert.deepEqual(seen, []);
    assert.equal(deliver({ jsonrpc: "2.0", id: 1, result: { real: true } }), true);
    assert.deepEqual(await p, { real: true });
  });

  test("timers are called as plain functions (browsers refuse setTimeout with a foreign this)", async () => {
    const thisValues = [];
    const client = new BridgeClient({
      post: () => {},
      parent: {},
      timeoutMs: 5,
      setTimer: function (fn, ms) { thisValues.push(this); return setTimeout(fn, ms); },
      clearTimer: function (t) { thisValues.push(this); clearTimeout(t); },
    });
    await assert.rejects(client.request("x"), (error) => error.code === "bridge_timeout");
    assert.ok(thisValues.length >= 1);
    for (const value of thisValues) assert.ok(value === undefined || value === globalThis, "no BridgeClient this");
    const real = new BridgeClient({ post: () => {}, parent: {}, timeoutMs: 5 });
    await assert.rejects(real.request("y"), (error) => error.code === "bridge_timeout");
  });

  test("the constructor needs post()", () => {
    assert.throws(() => new BridgeClient({}), TypeError);
  });
});

describe("globalThis.AutonomProbeCore", () => {
  test("mirrors every export of the module", () => {
    const g = globalThis.AutonomProbeCore;
    assert.ok(g && Object.isFrozen(g));
    for (const [name, value] of Object.entries(core)) {
      if (name === "default") continue;
      assert.equal(g[name], value, `AutonomProbeCore.${name}`);
    }
    for (const name of ["CHECKS", "createRun", "setStatus", "blockDependants", "resultsJson", "summarize", "BridgeClient",
      "parseFrameHeader", "median", "p95", "originOf", "wsOriginFor", "redact"]) {
      assert.ok(name in g, name);
    }
  });

  test("the core source has no imports and cannot close an inline script", () => {
    assert.ok(!/^\s*import\s/m.test(CORE_SOURCE), "no static imports");
    assert.ok(!/\bimport\s*\(/.test(CORE_SOURCE), "no dynamic imports");
    assert.ok(!/<\/script/i.test(CORE_SOURCE), "no </script");
    assert.ok(!CORE_SOURCE.includes("<!-- PROBE_CORE -->"));
  });
});

describe("probe-page.html", () => {
  const viewScript = (() => {
    const matches = [...PAGE.matchAll(/<script type="module">\n([\s\S]*?)<\/script>/g)];
    return matches.length === 1 ? matches[0][1] : null;
  })();

  test("is a complete English document with a viewport meta", () => {
    assert.ok(PAGE.startsWith("<!doctype html>"));
    assert.match(PAGE, /<html lang="en"/);
    assert.match(PAGE, /<meta name="viewport" content="width=device-width[^"]*">/);
    assert.match(PAGE, /<title>Autonom probe<\/title>/);
  });

  test("has exactly one core marker and one config placeholder inside the JSON element", () => {
    assert.equal(PAGE.split("<!-- PROBE_CORE -->").length - 1, 1);
    assert.match(PAGE, /^<!-- PROBE_CORE -->$/m, "the marker is a whole line");
    assert.equal(PAGE.split("__PROBE_CONFIG_JSON__").length - 1, 1);
    assert.match(PAGE, /<script type="application\/json" id="probe-config">__PROBE_CONFIG_JSON__<\/script>/);
    assert.ok(PAGE.indexOf("id=\"probe-config\"") < PAGE.indexOf("<!-- PROBE_CORE -->"));
    assert.ok(PAGE.indexOf("<!-- PROBE_CORE -->") < PAGE.indexOf("<script type=\"module\">"), "core is inlined before the view script");
    assert.ok(viewScript, "exactly one view module script");
  });

  test("loads no external resource from any domain", () => {
    assert.ok(!/<link\b/i.test(PAGE), "no <link>");
    assert.ok(!/@import/i.test(PAGE), "no @import");
    assert.ok(!/\b(?:src|href|action|poster|srcset)\s*=\s*["']?\s*(?:https?:)?\/\//i.test(PAGE), "no src/href to a URL");
    assert.ok(!/url\(\s*["']?\s*(?:https?:)?\/\//i.test(PAGE), "no CSS url() to a URL");
    assert.ok(!/<(?:img|iframe|object|embed|video|audio|source)\b/i.test(PAGE), "no embedded media elements");
    assert.ok(!/https:\/\//.test(PAGE), "no https URL at all");
    assert.ok(!/fonts\.|googleapis|cdn\./i.test(PAGE));
  });

  test("names all 27 check ids", () => {
    for (const id of EXPECTED_IDS) assert.ok(PAGE.includes(`"${id}"`), id);
  });

  test("has the summary live region, the action buttons and the manual controls", () => {
    assert.match(PAGE, /id="pb-summary" role="status" aria-live="polite"/);
    assert.equal(PAGE.split('aria-live="polite"').length - 1, 1, "only the summary is live");
    assert.match(PAGE, /<button[^>]*id="pb-copy"[^>]*>Copy results JSON<\/button>/);
    assert.match(PAGE, /<button[^>]*id="pb-send"[^>]*>Send to chat<\/button>/);
    assert.match(PAGE, /<button[^>]*class="btn primary pb-run"[^>]*>Run all<\/button>/);
    assert.match(PAGE, /<button[^>]*id="pb-run-devices"[^>]*>Run device checks<\/button>/);
    assert.match(viewScript, /text: "Passed"/);
    assert.match(viewScript, /text: "Failed"/);
    assert.match(viewScript, /Running… \$\{done\}/);
    assert.match(viewScript, /"Run again"/);
    assert.match(viewScript, /Copy blocked/);
    assert.match(PAGE, /Press ⌘C to copy\./);
    assert.match(PAGE, /<textarea id="pb-json" rows="8" readonly/);
    assert.match(PAGE, /27 checks ready\. Run all takes about 30 s\./);
    assert.match(viewScript, /What is the Autonom probe nonce\?/);
  });

  test("rows are disclosure buttons and the remote picker offers no free-text URL", () => {
    assert.match(viewScript, /"aria-expanded": "false", "aria-controls": detailId/);
    assert.match(PAGE, /id="pb-pick" aria-haspopup="menu" aria-expanded="false" aria-controls="pb-menu"/);
    assert.ok(!/<input\b/i.test(PAGE), "no input fields at all");
    assert.match(viewScript, /"None"/);
  });

  test("motion limits and reduced motion", () => {
    assert.match(PAGE, /@media \(prefers-reduced-motion:reduce\)\{[^}]*animation:none!important;transition:none!important\}/);
    assert.match(PAGE, /\.pb-spin::after\{content:"\\2026"\}/);
    assert.match(PAGE, /transform 160ms var\(--ease-out\)/);
    assert.match(PAGE, /pb-fade 160ms var\(--ease-out\)/);
  });

  test("never stores the token in localStorage, the model context or the results", () => {
    assert.ok(!/localStorage|sessionStorage|indexedDB|document\.cookie/.test(viewScript));
    assert.match(viewScript, /C\.scrubForExport\(C\.resultsJson\(/);
    assert.match(viewScript, /C\.contextParams\(S\.nonce, S\.selection\)/);
    assert.ok(!/contextParams\([^)]*token/i.test(viewScript));
  });

  test("the inline view script parses as a module", () => {
    const dir = mkdtempSync(join(tmpdir(), "probe-ui-"));
    try {
      const file = join(dir, "view.mjs");
      writeFileSync(file, viewScript);
      const result = spawnSync(process.execPath, ["--check", file], { encoding: "utf8" });
      assert.equal(result.status, 0, result.stderr);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("the server's assembly (2.8) yields one inlined core and the config JSON", () => {
    const configJson = JSON.stringify({ probe_version: "0.1.0", local_origin: "http://127.0.0.1:47811", remote_routes: [{ label: "x</script>", origin: "http://127.0.0.1:47812" }], allowed_devices: [], media_port: 47811 })
      .replace(/<\//g, "<\\/");
    const assembled = PAGE.replace("<!-- PROBE_CORE -->", `<script type="module">\n${CORE_SOURCE}\n</script>`).replace("__PROBE_CONFIG_JSON__", configJson);
    assert.ok(!assembled.includes("__PROBE_CONFIG_JSON__"));
    assert.ok(!assembled.includes("<!-- PROBE_CORE -->"));
    assert.equal(assembled.split("<script type=\"module\">").length - 1, 2);
    const configText = assembled.match(/<script type="application\/json" id="probe-config">([\s\S]*?)<\/script>/)[1];
    assert.equal(JSON.parse(configText).remote_routes[0].label, "x</script>");
  });

  test("copies the Canvas tokens verbatim (light and dark) plus the four probe tokens", () => {
    const props = (block) => Object.fromEntries([...block.matchAll(/(--[a-z0-9-]+):([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
    const styles = workspaceStyles();
    const canvasLight = Object.assign({}, ...[...styles.matchAll(/^:root\{([^}]+)\}/gm)].map((match) => props(match[1] + ";")));
    const canvasDark = props(styles.match(/@media \(prefers-color-scheme:dark\)\{:root\{([\s\S]*?)\n\}\}/)[1]);
    assert.ok(Object.keys(canvasLight).length >= 20 && Object.keys(canvasDark).length >= 15);
    const pageLight = props(PAGE.match(/^:root\{([\s\S]*?)\n\}/m)[1]);
    const pageDarkMedia = props(PAGE.match(/@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme=light\]\)\{([\s\S]*?)\n\}\}/)[1]);
    const pageDarkAttr = props(PAGE.match(/^:root\[data-theme=dark\]\{([\s\S]*?)\n\}/m)[1]);
    for (const [name, value] of Object.entries(canvasLight)) assert.equal(pageLight[name], value, `light ${name}`);
    for (const [name, value] of Object.entries(canvasDark)) {
      assert.equal(pageDarkMedia[name], value, `dark (media) ${name}`);
      assert.equal(pageDarkAttr[name], value, `dark (data-theme) ${name}`);
    }
    assert.deepEqual(Object.keys(pageDarkMedia).sort(), Object.keys(canvasDark).sort());
    assert.deepEqual(Object.keys(pageDarkAttr).sort(), Object.keys(canvasDark).sort());
    assert.equal(pageLight["--pass"], "var(--live)");
    assert.equal(pageLight["--fail"], "var(--danger)");
    assert.equal(pageLight["--skip"], "var(--idle)");
    assert.equal(pageLight["--ease-out"], "cubic-bezier(.23,1,.32,1)");
    assert.deepEqual(Object.keys(pageLight).sort(), [...Object.keys(canvasLight), "--pass", "--fail", "--skip", "--ease-out"].sort());
  });

  test("narrow and wide layouts follow DESIGN 3", () => {
    assert.match(PAGE, /@media \(min-width:900px\)\{[\s\S]*grid-template-columns:minmax\(0,1fr\) 340px[\s\S]*\.pb-side\{position:sticky;top:68px\}/);
    assert.match(PAGE, /grid-template-columns:minmax\(0,1fr\) 80px 80px 16px/);
    assert.match(PAGE, /grid-template-columns:minmax\(0,1fr\) 96px 96px 20px/);
    assert.match(PAGE, /aspect-ratio:9\/19\.5/);
    assert.match(PAGE, /@media \(pointer:coarse\)\{\.pb-row\{min-height:44px\}\}/);
  });
});
