// Unit tests of the Autonom (dev) diagnostics page (contract run ws-p3-mcp-adapter, section 6, p3-015):
// the pure functions of workspace-dev-core.mjs (frame parsing, Annex B chunks, points, percentiles, rate,
// pull state, measurements, diagnostics), the bridge client, the HTML markers the server needs (2.12), the
// no-network scan, and one page run against a fake document, a fake host bridge and a fake WebCodecs
// decoder. No browser, host, server, network or device is involved and no file is written.
import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import test, { describe } from "node:test";
import { fileURLToPath } from "node:url";

import * as core from "../integrations/chatgpt/autonom-chatgpt/ui/workspace-dev-core.mjs";

const {
  AGENT_HAS_CONTROL, BridgeClient, BridgeError, CHECK_IDS, DIAG_RUN_ID_PATTERN, agentHasControl, annexBChunks,
  base64ToBytes, buildDiagnostics, codecFromSps, controlView, createMeter, detectEntry, dropSecrets, emptyChecks,
  fpsCounter, framesArgs, initializeParams, looksSecretOrPath, makeRunId, mediaCheck, nextPullState, normalizePoint,
  parseFramesResult, parseOpenStreamResult, percentile, readConfig, readHostId, readSessions, recordPull, setCheck,
  startPage, summarizeMeter, systemEvent, tapEvent, toolOutcome,
} = core;

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const UI_DIR = join(ROOT, "integrations/chatgpt/autonom-chatgpt/ui");
const PAGE_PATH = join(UI_DIR, "workspace-dev.html");
const CORE_PATH = join(UI_DIR, "workspace-dev-core.mjs");
const PAGE = readFileSync(PAGE_PATH, "utf8");
const CORE_SOURCE = readFileSync(CORE_PATH, "utf8");

const HOST_ID = "h_abcdefghijklmnopqrstuvwxyz";
const SESSION_ID = "s_0123456789";
const STREAM_ID = "st_0123456789abcdef";
// A real H.264 baseline SPS (profile 0x42, constraints 0xC0, level 0x1E) and PPS, Annex B with start codes.
const SPS = [0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x1e, 0xd9, 0x00, 0xa0, 0x47, 0xfe, 0xc8];
const PPS = [0, 0, 0, 1, 0x68, 0xce, 0x38, 0x80];
const CONFIG = Uint8Array.from([...SPS, ...PPS]);
const IDR = Uint8Array.from([0, 0, 0, 1, 0x65, 0x88, 0x84, 0x00, 0x33]);
const DELTA = Uint8Array.from([0, 0, 0, 1, 0x41, 0x9a, 0x02, 0x03]);
const b64 = (bytes) => Buffer.from(bytes).toString("base64");

function envelope(tool, fields = {}, { ok = true, meta } = {}) {
  const sc = {
    schema_version: "autonom.mcp/v1", operation_id: "op_0123456789abcdef", tool, ok,
    outcome: ok ? "completed" : "rejected", host_id: HOST_ID, warnings: [], ...fields,
  };
  const result = { structuredContent: sc, content: [{ type: "text", text: JSON.stringify(sc) }], isError: !ok };
  if (meta) result._meta = meta;
  return result;
}

function failure(tool, code, fields = {}) {
  return envelope(tool, { error_code: code, error: `${code} happened`, hint: null, retry_safe: true, ...fields }, { ok: false });
}

function streamObj(fields = {}) {
  return {
    stream_id: STREAM_ID, session: { host_id: HOST_ID, session_id: SESSION_ID }, profile: "focus", state: "open",
    width: 1080, height: 2400, codec: "avc1.42C01E", epoch: 1, buffer_bytes: 1000, ...fields,
  };
}

function framesResult(frames, { config = CONFIG, resync = false, epoch = 1, dropped = 0, stream = {}, extra = {} } = {}) {
  const list = frames.map(([seq, key, data, pts]) => ({ seq, key, pts: pts ?? seq * 16666, data_b64: b64(data) }));
  const bytes = frames.reduce((n, f) => n + f[2].length, 0);
  return envelope("get_frames", {
    stream: streamObj({ epoch, ...stream }), frames: list.length, bytes,
    first_seq: list.length ? list[0].seq : null, last_seq: list.length ? list[list.length - 1].seq : null,
    resync, dropped, server_ms: 3, ...extra,
  }, { meta: { "autonom/frames": { config_b64: config ? b64(config) : null, frames: list } } });
}

// ------------------------------------------------------------------------------------------ markers

describe("workspace-dev.html and the core module", () => {
  test("the page carries the markers the server assembles (contract 2.12)", () => {
    assert.equal(PAGE.split("<!-- WORKSPACE_CORE -->").length - 1, 1);
    assert.equal(PAGE.split("__WORKSPACE_CONFIG_JSON__").length - 1, 1);
    assert.match(PAGE, /<script\b[^>]*\bid="workspace-config"[^>]*>[^<]*__WORKSPACE_CONFIG_JSON__/);
    assert.match(PAGE, /<main id="workspace"/);
    assert.doesNotMatch(CORE_SOURCE, /<\/script/i);
    assert.doesNotMatch(CORE_SOURCE, /<!--|<script/i, "inline module data must not open comments or scripts");
    assert.doesNotMatch(CORE_SOURCE, /__WORKSPACE_CONFIG_JSON__|WORKSPACE_CORE -->/);
  });

  test("assembling the page like the server yields one inline module and the config", () => {
    const config = JSON.stringify({ adapter_version: "0.1.0", schema_version: "autonom.mcp/v1" });
    const html = PAGE.replace("__WORKSPACE_CONFIG_JSON__", config)
      .replace("<!-- WORKSPACE_CORE -->", `<script type="module">\n${CORE_SOURCE}\n</script>`);
    assert.equal(html.split("<script").length - 1, 2);
    assert.equal(html.split("</script>").length - 1, 2);
    const fake = { getElementById: (id) => (id === "workspace-config" ? { textContent: config } : null) };
    assert.deepEqual(readConfig(fake), { adapter_version: "0.1.0", schema_version: "autonom.mcp/v1" });
    assert.equal(readConfig({ getElementById: () => ({ textContent: "{" }) }), null);
    assert.equal(readConfig({ getElementById: () => ({ textContent: "[1]" }) }), null);
    assert.equal(readConfig(null), null);
  });

  test("no network API and no external resource in the page files (p3-015-S02)", () => {
    for (const [name, source] of [["workspace-dev.html", PAGE], ["workspace-dev-core.mjs", CORE_SOURCE]]) {
      for (const needle of ["fetch(", "new WebSocket", "WebSocket(", "EventSource", "XMLHttpRequest", "sendBeacon", "import(",
        "new Worker", "importScripts", "RTCPeerConnection"]) {
        assert.ok(!source.includes(needle), `${name} contains ${needle}`);
      }
      assert.doesNotMatch(source, /\b(?:src|href)\s*=\s*["']?(?:https?:)?\/\//i, `${name} loads an external resource`);
      assert.doesNotMatch(source, /@import|url\(\s*["']?https?:/i, `${name} loads external CSS`);
      assert.doesNotMatch(source, /^\s*import\s/m, `${name} imports a module`);
    }
  });

  test("every element id the view uses exists in the page, and the files stay small", () => {
    const used = new Set([...CORE_SOURCE.matchAll(/\$\("([a-z0-9-]+)"\)/g)].map((m) => m[1]));
    for (const match of CORE_SOURCE.matchAll(/\bon\("([a-z0-9-]+)"/g)) used.add(match[1]);
    for (const match of CORE_SOURCE.matchAll(/\btext\("([a-z0-9-]+)"/g)) used.add(match[1]);
    for (const match of CORE_SOURCE.matchAll(/\["start-focus"[^\]]*\]/g)) {
      for (const id of match[0].matchAll(/"([a-z0-9-]+)"/g)) used.add(id[1]);
    }
    assert.ok(used.size >= 20, `only ${used.size} ids found`);
    for (const id of used) assert.match(PAGE, new RegExp(`id="${id}"`), `missing #${id}`);
    for (const path of [PAGE_PATH, CORE_PATH]) assert.ok(statSync(path).size < 256 * 1024, path);
  });

  test("importing the core in Node runs no page code", () => {
    assert.equal(typeof globalThis.document, "undefined");
    assert.equal(core.WORKSPACE_CORE_VERSION, "0.1.0");
    assert.deepEqual([...CHECK_IDS], ["bridge.list_sessions", "media.focus", "media.thumb", "input.tap", "control.pause", "control.resume"]);
  });
});

// ------------------------------------------------------------------------------------------ parsing

describe("parseFramesResult", () => {
  test("reads the structured fields and decodes the frames in seq order", () => {
    const parsed = parseFramesResult(framesResult([[4, false, DELTA], [3, true, IDR]]));
    assert.equal(parsed.ok, true);
    assert.equal(parsed.errorCode, null);
    assert.deepEqual(parsed.frames.map((f) => [f.seq, f.key]), [[3, true], [4, false]]);
    assert.deepEqual([...parsed.frames[0].data], [...IDR]);
    assert.deepEqual([...parsed.config], [...CONFIG]);
    assert.equal(parsed.epoch, 1);
    assert.equal(parsed.codec, "avc1.42C01E");
    assert.deepEqual([parsed.width, parsed.height], [1080, 2400]);
    assert.equal(parsed.bytes, IDR.length + DELTA.length);
    assert.equal(parsed.lastSeq, 3, "last_seq comes from the structured field");
    assert.equal(parsed.resync, false);
    assert.equal(parsed.dropped, 0);
    assert.equal(parsed.serverMs, 3);
    assert.equal(parsed.invalid, 0);
    assert.equal(parsed.countMismatch, false);
    assert.equal(parsed.stream.stream_id, STREAM_ID);
  });

  test("a resync carries width, height and the config", () => {
    const parsed = parseFramesResult(framesResult([[90, true, IDR]], { resync: true, epoch: 2, dropped: 7, extra: { width: 720, height: 1280 } }));
    assert.equal(parsed.resync, true);
    assert.equal(parsed.epoch, 2);
    assert.equal(parsed.dropped, 7);
    assert.deepEqual([parsed.width, parsed.height], [720, 1280]);
  });

  test("an empty answer and a missing config are fine", () => {
    const parsed = parseFramesResult(framesResult([], { config: null }));
    assert.equal(parsed.ok, true);
    assert.deepEqual(parsed.frames, []);
    assert.equal(parsed.config, null);
    assert.equal(parsed.lastSeq, null);
    assert.equal(parsed.firstSeq, null);
  });

  test("invalid entries are skipped and counted", () => {
    const result = framesResult([[5, true, IDR]]);
    result._meta["autonom/frames"].frames.push(
      { seq: 0, key: false, pts: 1, data_b64: b64(DELTA) },
      { seq: 6, key: false, pts: 1, data_b64: "!!!" },
      { seq: 5, key: false, pts: 1, data_b64: b64(DELTA) },
      { seq: 7, key: false, pts: 1, data_b64: "" },
      "junk",
      { seq: 1.5, key: false, data_b64: b64(DELTA) },
    );
    result._meta["autonom/frames"].config_b64 = "%%%";
    const parsed = parseFramesResult(result);
    assert.deepEqual(parsed.frames.map((f) => f.seq), [5]);
    assert.equal(parsed.invalid, 7);
    assert.equal(parsed.config, null);
    assert.equal(parsed.countMismatch, false);
  });

  test("a frame count that disagrees with the payload is flagged", () => {
    const result = framesResult([[5, true, IDR]]);
    result.structuredContent.frames = 2;
    assert.equal(parseFramesResult(result).countMismatch, true);
  });

  test("failures keep the error code, error and hint", () => {
    const parsed = parseFramesResult(failure("get_frames", "stream_not_found", { hint: "Open a new stream" }));
    assert.equal(parsed.ok, false);
    assert.equal(parsed.errorCode, "stream_not_found");
    assert.equal(parsed.error, "stream_not_found happened");
    assert.equal(parsed.hint, "Open a new stream");
    assert.deepEqual(parsed.frames, []);
  });

  test("malformed results never throw", () => {
    for (const value of [null, undefined, 3, "x", [], {}, { structuredContent: [] }, { content: [{ text: "{" }] }, { _meta: 5 }]) {
      const parsed = parseFramesResult(value);
      assert.equal(parsed.ok, false);
      assert.equal(parsed.errorCode, "malformed_result");
      assert.deepEqual(parsed.frames, []);
    }
  });

  test("the text content is used when structured content is missing", () => {
    const result = framesResult([[2, true, IDR]]);
    delete result.structuredContent;
    const parsed = parseFramesResult(result);
    assert.equal(parsed.ok, true);
    assert.equal(parsed.lastSeq, 2);
  });

  test("isError true wins over ok true", () => {
    const result = framesResult([[2, true, IDR]]);
    result.isError = true;
    assert.equal(parseFramesResult(result).ok, false);
  });
});

describe("parseOpenStreamResult and other readers", () => {
  test("open_stream: the stream object, or the failure", () => {
    const opened = parseOpenStreamResult(envelope("open_stream", { stream: streamObj({ profile: "thumb" }) }));
    assert.equal(opened.ok, true);
    assert.equal(opened.stream.profile, "thumb");
    const refused = parseOpenStreamResult(failure("open_stream", "stream_limit"));
    assert.equal(refused.ok, false);
    assert.equal(refused.errorCode, "stream_limit");
    const badId = parseOpenStreamResult(envelope("open_stream", { stream: streamObj({ stream_id: "st_x" }) }));
    assert.equal(badId.ok, false);
    assert.equal(badId.errorCode, "malformed_result");
  });

  test("toolOutcome, controlView and agentHasControl", () => {
    const refused = toolOutcome(failure("send_input", "control_held", {
      control: { state: "held", holder: { kind: "agent", id: `chatgpt-agent:${HOST_ID}`, label: "ChatGPT agent" }, generation: 4, role: "agent", mine: true },
    }));
    assert.equal(refused.ok, false);
    assert.deepEqual(controlView(refused.sc), {
      state: "held", holder: { kind: "agent", id: `chatgpt-agent:${HOST_ID}`, label: "ChatGPT agent" }, generation: 4, role: "agent", mine: true,
    });
    assert.equal(agentHasControl(refused), true);
    assert.equal(agentHasControl(toolOutcome(failure("send_input", "control_held", { control: { state: "held", role: "human" } }))), false);
    assert.equal(agentHasControl(toolOutcome(failure("send_input", "input_failed"))), false);
    assert.equal(agentHasControl(toolOutcome(envelope("send_input", { control: { role: "agent" } }))), false, "never for completed input");
    assert.equal(agentHasControl(null), false);
    assert.equal(controlView({}), null);
    assert.equal(AGENT_HAS_CONTROL, "The agent has control - Pause to take over");
  });

  test("readSessions keeps entries with valid ids", () => {
    const result = envelope("list_sessions", { sessions: [
      { host_id: HOST_ID, session_id: SESSION_ID, platform: "android", target_id: "emulator-5584", label: "a", live: true },
      { host_id: HOST_ID, session_id: "bad", platform: "android" },
      { host_id: "h_bad", session_id: "s_1" },
      { host_id: HOST_ID, session_id: "s_2", platform: "web", live: false },
      7,
    ] });
    const { ok, sessions } = readSessions(result);
    assert.equal(ok, true);
    assert.deepEqual(sessions.map((s) => [s.session_id, s.platform, s.live]), [[SESSION_ID, "android", true], ["s_2", null, false]]);
    const fromWorkspace = readSessions(envelope("open_workspace", { workspace: { sessions: [{ host_id: HOST_ID, session_id: SESSION_ID }] } }));
    assert.equal(fromWorkspace.sessions.length, 1);
    assert.deepEqual(readSessions(null), { ok: false, sessions: [] });
  });

  test("readHostId from list_hosts and open_workspace", () => {
    assert.equal(readHostId(envelope("list_hosts", { host_id: null, hosts: [{ host_id: HOST_ID, kind: "local" }] })), HOST_ID);
    assert.equal(readHostId(envelope("open_workspace", { host_id: null, workspace: { host_id: HOST_ID, hosts: [] } })), HOST_ID);
    assert.equal(readHostId(envelope("list_hosts", { host_id: "nope", hosts: [{ host_id: "h_x", kind: "local" }] })), null);
  });

  test("detectEntry and initializeParams", () => {
    assert.equal(detectEntry({ entrypoint: "global" }), "global");
    assert.equal(detectEntry({ entryPoint: { type: "thread" } }), "thread");
    assert.equal(detectEntry({ location: "sidebar" }), "global");
    assert.equal(detectEntry({}), "unknown");
    assert.equal(detectEntry(null), "unknown");
    const params = initializeParams("9.9.9");
    assert.equal(params.protocolVersion, "2026-01-26");
    assert.deepEqual(params.appInfo, { name: "autonom-workspace-dev", version: "9.9.9" });
  });
});

// ------------------------------------------------------------------------------------------ H.264

describe("codecFromSps, base64ToBytes and annexBChunks", () => {
  test("the codec string comes from the SPS (3- and 4-byte start codes)", () => {
    assert.equal(codecFromSps(CONFIG), "avc1.42C01E");
    assert.equal(codecFromSps(Uint8Array.from([0, 0, 1, 0x67, 0x64, 0x00, 0x28, 0xac])), "avc1.640028");
    assert.equal(codecFromSps(Uint8Array.from([...PPS, ...SPS])), "avc1.42C01E", "the SPS can follow other NAL units");
    assert.equal(codecFromSps(Uint8Array.from(PPS)), null);
    assert.equal(codecFromSps(Uint8Array.from([0, 0, 1, 0x67, 0x42])), null, "a truncated SPS");
    assert.equal(codecFromSps([0, 0, 1, 0x67, 0x42, 0, 0x1e]), null, "only Uint8Array");
  });

  test("emulation-prevention bytes are skipped", () => {
    // NAL 0x67, profile 0x00, constraints 0x00, then 03 1F: the 03 after two zero bytes is an emulation-prevention byte.
    assert.equal(codecFromSps(Uint8Array.from([0, 0, 0, 1, 0x67, 0x00, 0x00, 0x03, 0x1f])), "avc1.00001F");
  });

  test("base64ToBytes takes standard and URL-safe text and refuses the rest", () => {
    assert.deepEqual([...base64ToBytes(b64(IDR))], [...IDR]);
    assert.deepEqual([...base64ToBytes(Buffer.from([251, 255, 191]).toString("base64url"))], [251, 255, 191]);
    assert.deepEqual([...base64ToBytes("AQ")], [1]);
    assert.deepEqual([...base64ToBytes("")], []);
    for (const bad of ["A", "A$==", 5, null, "Zm9v\n"]) assert.equal(base64ToBytes(bad), null, String(bad));
    const big = Uint8Array.from({ length: 70000 }, (_, i) => (i * 31) & 255);
    assert.deepEqual(Buffer.from(base64ToBytes(b64(big))), Buffer.from(big));
  });

  test("the config is prepended to every key frame and never to delta frames", () => {
    const frames = [{ seq: 1, key: true, pts: 100.4, data: IDR }, { seq: 2, key: false, pts: 200, data: DELTA }, { seq: 3, key: true, pts: -5, data: IDR }];
    const chunks = annexBChunks(frames, CONFIG);
    assert.deepEqual(chunks.map((c) => [c.seq, c.type, c.timestamp]), [[1, "key", 100], [2, "delta", 200], [3, "key", 0]]);
    assert.deepEqual([...chunks[0].data], [...CONFIG, ...IDR]);
    assert.deepEqual([...chunks[1].data], [...DELTA]);
    assert.deepEqual([...chunks[2].data], [...CONFIG, ...IDR]);
    assert.deepEqual([...frames[0].data], [...IDR], "the input is not changed");
    assert.deepEqual([...annexBChunks(frames, null)[0].data], [...IDR], "no config: the key frame passes as is");
    assert.deepEqual(annexBChunks([null, { seq: 4, key: true, data: new Uint8Array() }, { seq: 5, key: false, data: [1] }], CONFIG), []);
    assert.deepEqual(annexBChunks(undefined, CONFIG), []);
  });

  test("parsed frames feed annexBChunks end to end", () => {
    const parsed = parseFramesResult(framesResult([[1, true, IDR], [2, false, DELTA]]));
    const chunks = annexBChunks(parsed.frames, parsed.config);
    assert.equal(codecFromSps(chunks[0].data), "avc1.42C01E");
    assert.equal(chunks[1].data.length, DELTA.length);
  });
});

// ------------------------------------------------------------------------------------------ pull state

describe("nextPullState and framesArgs", () => {
  const start = { afterSeq: 0, epoch: null, config: null };

  test("the first answer sets epoch, config and after_seq without counting a resync", () => {
    const next = nextPullState(start, parseFramesResult(framesResult([[1, true, IDR], [2, false, DELTA]])));
    assert.equal(next.afterSeq, 2);
    assert.equal(next.epoch, 1);
    assert.deepEqual([...next.config], [...CONFIG]);
    assert.equal(next.reconfigure, true);
    assert.equal(next.resynced, false);
    assert.deepEqual(framesArgs(STREAM_ID, next), { stream_id: STREAM_ID, after_seq: 2, wait_ms: 250, epoch: 1 });
  });

  test("the same config again does not reconfigure; an empty answer keeps after_seq", () => {
    const one = nextPullState(start, parseFramesResult(framesResult([[1, true, IDR]])));
    const two = nextPullState(one, parseFramesResult(framesResult([[2, false, DELTA]])));
    assert.equal(two.reconfigure, false);
    assert.equal(two.afterSeq, 2);
    const three = nextPullState(two, parseFramesResult(framesResult([], { config: null })));
    assert.equal(three.afterSeq, 2);
    assert.equal(three.reconfigure, false);
    assert.equal(three.config, two.config, "the config is kept when the answer has none");
  });

  test("a resync moves after_seq to the delivered key frame and reconfigures", () => {
    const one = nextPullState(start, parseFramesResult(framesResult([[1, true, IDR], [2, false, DELTA]])));
    const resync = nextPullState(one, parseFramesResult(framesResult([[400, true, IDR]], { resync: true })));
    assert.equal(resync.afterSeq, 400);
    assert.equal(resync.reconfigure, true);
    assert.equal(resync.resynced, true);
  });

  test("a new epoch is followed even when seq starts again from 1", () => {
    const one = nextPullState(start, parseFramesResult(framesResult([[50, true, IDR]])));
    const next = nextPullState(one, parseFramesResult(framesResult([[1, true, IDR]], { epoch: 2, resync: true })));
    assert.equal(next.epoch, 2);
    assert.equal(next.afterSeq, 1);
    assert.equal(next.resynced, true);
    const noFlag = nextPullState(one, parseFramesResult(framesResult([[1, true, IDR]], { epoch: 3 })));
    assert.equal(noFlag.afterSeq, 1, "an epoch change alone also restarts after_seq");
    assert.equal(noFlag.reconfigure, true);
  });

  test("empty resync answers before the key frame mark one pending resync and do not reset the decoder", () => {
    // A Canvas Display menu change sends a new session message: epoch 1 -> 2, the buffer is cleared, and the
    // server answers resync:true with no frames until the encoder's first key frame is buffered.
    const meter = createMeter("focus", 0);
    let state = { afterSeq: 0, epoch: null, config: null };
    const pull = (result) => {
      const parsed = parseFramesResult(result);
      const next = nextPullState(state, parsed);
      recordPull(meter, parsed, 5, state.afterSeq, next.resynced);
      state = next;
      return next;
    };
    pull(framesResult([[39, true, IDR], [40, false, DELTA]]));
    assert.equal(state.afterSeq, 40);
    const empties = [];
    for (let i = 0; i < 3; i += 1) empties.push(pull(framesResult([], { resync: true, epoch: 2, extra: { last_seq: 40 } })));
    for (const empty of empties) {
      assert.equal(empty.reconfigure, false, "an empty resync answer must not close and reconfigure the decoder");
      assert.equal(empty.resynced, false);
      assert.equal(empty.pendingResync, true);
    }
    assert.equal(meter.resyncs, 0, "nothing is counted before the key frame arrives");
    const keyed = pull(framesResult([[1, true, IDR]], { resync: true, epoch: 2 }));
    assert.equal(keyed.reconfigure, true);
    assert.equal(keyed.resynced, true);
    assert.equal(keyed.pendingResync, false);
    assert.equal(keyed.afterSeq, 1);
    assert.equal(meter.resyncs, 1, "one new session counts as one resync");
    pull(framesResult([[2, false, DELTA]], { epoch: 2 }));
    assert.equal(meter.resyncs, 1);
    assert.equal(meter.gaps, 0);
  });

  test("a pending epoch change without the resync flag still reconfigures once frames arrive", () => {
    const one = nextPullState({ afterSeq: 0, epoch: null, config: null }, parseFramesResult(framesResult([[50, true, IDR]])));
    const empty = nextPullState(one, parseFramesResult(framesResult([], { epoch: 2 })));
    assert.equal(empty.reconfigure, false);
    assert.equal(empty.pendingResync, true);
    const keyed = nextPullState(empty, parseFramesResult(framesResult([[60, true, IDR]], { epoch: 2 })));
    assert.equal(keyed.reconfigure, true);
    assert.equal(keyed.resynced, true);
  });

  test("recordPull without the flag ignores empty resync answers", () => {
    const meter = createMeter("focus", 0);
    for (let i = 0; i < 3; i += 1) recordPull(meter, parseFramesResult(framesResult([], { resync: true })), 5, 40);
    assert.equal(meter.resyncs, 0);
    recordPull(meter, parseFramesResult(framesResult([[90, true, IDR]], { resync: true })), 5, 40);
    assert.equal(meter.resyncs, 1);
  });

  test("a changed config reconfigures without a resync", () => {
    const one = nextPullState(start, parseFramesResult(framesResult([[1, true, IDR]])));
    const other = Uint8Array.from([0, 0, 0, 1, 0x67, 0x64, 0x00, 0x28, 0xac]);
    const next = nextPullState(one, parseFramesResult(framesResult([[2, true, IDR]], { config: other })));
    assert.equal(next.reconfigure, true);
    assert.equal(next.resynced, false);
    assert.deepEqual([...next.config], [...other]);
  });

  test("a failed answer leaves the state unchanged", () => {
    const one = nextPullState(start, parseFramesResult(framesResult([[9, true, IDR]])));
    const next = nextPullState(one, parseFramesResult(failure("get_frames", "cli_timeout")));
    assert.deepEqual([next.afterSeq, next.epoch, next.reconfigure], [9, 1, false]);
    assert.deepEqual(nextPullState(undefined, undefined), {
      afterSeq: 0, epoch: null, config: null, reconfigure: false, resynced: false, pendingResync: false, pendingCounts: false,
    });
  });

  test("framesArgs: no epoch before the first answer, wait and budget options", () => {
    assert.deepEqual(framesArgs(STREAM_ID, start), { stream_id: STREAM_ID, after_seq: 0, wait_ms: 250 });
    assert.deepEqual(framesArgs(STREAM_ID, { afterSeq: -3, epoch: 0 }, { waitMs: 1000, maxBytes: 65536 }),
      { stream_id: STREAM_ID, after_seq: 0, wait_ms: 1000, epoch: 0, max_bytes: 65536 });
  });
});

// ------------------------------------------------------------------------------------------ input

describe("normalizePoint and input events", () => {
  const rect = { left: 10, top: 20, width: 200, height: 400 };

  test("maps the picture corners and centre to 0..1", () => {
    assert.deepEqual(normalizePoint(110, 220, rect, 1080, 2160), { x: 0.5, y: 0.5 });
    assert.deepEqual(normalizePoint(10, 20, rect, 1080, 2160), { x: 0, y: 0 });
    assert.deepEqual(normalizePoint(210, 420, rect, 1080, 2160), { x: 1, y: 1 });
    assert.deepEqual(normalizePoint(60, 120, rect), { x: 0.25, y: 0.25 }, "without a video size the box is the picture");
  });

  test("accounts for letterboxing (object-fit: contain)", () => {
    // A landscape 2000x1000 picture in a 200x400 box: drawn 200x100, centred vertically (top bar 150).
    assert.deepEqual(normalizePoint(110, 220, rect, 2000, 1000), { x: 0.5, y: 0.5 });
    assert.deepEqual(normalizePoint(10, 170, rect, 2000, 1000), { x: 0, y: 0 });
    assert.equal(normalizePoint(110, 100, rect, 2000, 1000), null, "inside the box but on the bar");
    // A tall picture in a wide box: pillarbox.
    assert.deepEqual(normalizePoint(150, 50, { left: 0, top: 0, width: 300, height: 100 }, 100, 100), { x: 0.5, y: 0.5 });
    assert.equal(normalizePoint(20, 50, { left: 0, top: 0, width: 300, height: 100 }, 100, 100), null);
  });

  test("refuses points outside and unusable input; rounds to 5 decimals", () => {
    assert.equal(normalizePoint(9, 220, rect, 1080, 2160), null);
    assert.equal(normalizePoint(110, 421, rect, 1080, 2160), null);
    for (const bad of [null, {}, { left: 0, top: 0, width: 0, height: 10 }, { left: "a", top: 0, width: 1, height: 1 }]) {
      assert.equal(normalizePoint(1, 1, bad, 10, 10), null);
    }
    assert.equal(normalizePoint(Number.NaN, 1, rect, 10, 10), null);
    const p = normalizePoint(10 + 200 / 3, 20, rect);
    assert.equal(p.x, 0.33333);
  });

  test("tap and system events have the send_input shapes", () => {
    assert.deepEqual(tapEvent({ x: 0.25, y: 0.75 }), { op: "tap", x: 0.25, y: 0.75 });
    assert.deepEqual(systemEvent("home"), { op: "system", button: "home" });
    assert.deepEqual(systemEvent("back"), { op: "system", button: "back" });
    assert.throws(() => systemEvent("power"), TypeError);
  });
});

// ------------------------------------------------------------------------------------------ numbers

describe("percentile and fpsCounter", () => {
  test("nearest-rank percentiles", () => {
    const values = [5, 1, 4, 2, 3, 6, 7, 8, 9, 10];
    assert.equal(percentile(values, 50), 5);
    assert.equal(percentile(values, 95), 10);
    assert.equal(percentile(values, 0), 1);
    assert.equal(percentile(values, 100), 10);
    assert.equal(percentile([7], 95), 7);
    assert.equal(percentile(Array.from({ length: 100 }, (_, i) => i + 1), 95), 95);
    assert.equal(percentile([], 50), null);
    assert.equal(percentile(null, 50), null);
    assert.equal(percentile([Number.NaN, "3", Infinity, 2], 50), 2, "only finite numbers count");
    assert.equal(percentile([1, 2, 3], 500), 3);
    assert.equal(percentile([1, 2, 3], Number.NaN), 2, "a bad p means the median");
    assert.deepEqual(values, [5, 1, 4, 2, 3, 6, 7, 8, 9, 10], "the input is not sorted in place");
  });

  test("the rate is events per second over the window", () => {
    const fps = fpsCounter(1000);
    assert.equal(fps.rate(0), 0);
    for (let t = 0; t < 2000; t += 50) fps.tick(t);
    assert.equal(fps.total, 40);
    assert.equal(fps.rate(1950), 20);
    assert.equal(fps.rate(2900), 1, "only the last tick is inside the window");
    assert.equal(fps.rate(2950), 0, "a tick exactly one window old is out");
    assert.equal(fps.rate(4000), 0);
    fps.reset();
    assert.equal(fps.total, 0);
    assert.equal(fps.rate(5000), 0);
  });

  test("before the window is full the rate uses the elapsed time", () => {
    const fps = fpsCounter(1000);
    fps.tick(0);
    fps.tick(100);
    fps.tick(200);
    assert.equal(fps.rate(500), 6);
    assert.equal(fps.rate(0), 0, "no elapsed time");
    fps.tick(Number.NaN);
    assert.equal(fps.total, 3);
    assert.equal(fpsCounter(-1).rate(0), 0);
  });
});

// ------------------------------------------------------------------------------------------ measurements

describe("meters and media checks", () => {
  test("recordPull counts frames, bytes, sizes, resyncs, drops and gaps", () => {
    const meter = createMeter("focus", 1000);
    recordPull(meter, parseFramesResult(framesResult([[1, true, IDR], [2, false, DELTA]])), 20, 0);
    recordPull(meter, parseFramesResult(framesResult([[5, false, DELTA]])), 30, 2);
    recordPull(meter, parseFramesResult(framesResult([[90, true, IDR]], { resync: true, dropped: 40 })), 40, 5);
    recordPull(meter, parseFramesResult(failure("get_frames", "cli_timeout")), 50, 90);
    assert.equal(meter.calls, 4);
    assert.equal(meter.call_errors, 1);
    assert.equal(meter.frames_received, 4);
    assert.equal(meter.key_frames, 2);
    assert.equal(meter.bytes, 2 * IDR.length + 2 * DELTA.length);
    assert.equal(meter.largest_packet, IDR.length);
    assert.equal(meter.key_frame_bytes, IDR.length);
    assert.equal(meter.resyncs, 1);
    assert.equal(meter.dropped_server, 40);
    assert.equal(meter.gaps, 1, "seq 3-4 were skipped without a resync");
    assert.equal(meter.error_code, "cli_timeout");
    assert.equal(meter.codec, "avc1.42C01E");
    meter.frames_painted = 30;
    meter.frame_age_ms.push(10, 20, 30, 40);
    meter.ended_at = 4000;
    const summary = summarizeMeter(meter, 99999);
    assert.equal(summary.duration_s, 3);
    assert.equal(summary.frames_per_s, 10);
    assert.equal(summary.call_ms_median, 30);
    assert.equal(summary.call_ms_p95, 50);
    assert.equal(summary.frame_age_ms_median, 20);
    assert.equal(summary.frame_age_ms_p95, 40);
    assert.equal(summary.bytes_per_s, Math.round((summary.bytes / 3) * 10) / 10);
    assert.equal(summary.dropped, 40);
    assert.equal(summarizeMeter(null, 0), null);
  });

  test("the first resync of a stream (after_seq 0) is not counted, and thumbs never count gaps", () => {
    const meter = createMeter("thumb", 0);
    recordPull(meter, parseFramesResult(framesResult([[30, true, IDR]], { resync: true })), 5, 0);
    recordPull(meter, parseFramesResult(framesResult([[90, true, IDR]])), 5, 30);
    assert.equal(meter.resyncs, 0);
    assert.equal(meter.gaps, 0);
    const empty = summarizeMeter(createMeter("focus", null), 10);
    assert.equal(empty.frames_per_s, null);
    assert.equal(empty.call_ms_median, null);
  });

  test("mediaCheck: pass after enough painted frames, fail on errors, else not exercised", () => {
    assert.equal(mediaCheck(null).status, "not_exercised");
    const focus = createMeter("focus", 0);
    assert.equal(mediaCheck(focus).status, "not_exercised");
    focus.frames_painted = 10;
    assert.equal(mediaCheck(focus).status, "pass");
    const failed = createMeter("focus", 0);
    failed.error_code = "media_unavailable";
    assert.equal(mediaCheck(failed).status, "fail");
    const thumb = createMeter("thumb", 0);
    thumb.frames_painted = 2;
    assert.equal(mediaCheck(thumb).status, "pass");
    const broken = createMeter("thumb", 0);
    broken.decoder_errors = 1;
    assert.match(mediaCheck(broken).detail, /decoder_error/);
  });

  test("setCheck refuses unknown ids and statuses", () => {
    const checks = emptyChecks();
    assert.deepEqual(Object.keys(checks), [...CHECK_IDS]);
    setCheck(checks, "input.tap", "pass", "x".repeat(500));
    assert.equal(checks["input.tap"].detail.length, 400);
    assert.throws(() => setCheck(checks, "input.swipe", "pass"), RangeError);
    assert.throws(() => setCheck(checks, "input.tap", "skipped"), RangeError);
  });
});

// ------------------------------------------------------------------------------------------ diagnostics

describe("buildDiagnostics", () => {
  const TOKEN = "Zk3pQ9vT2mXc8LrB7nW4yH6sJ1dF5gA0eK";
  const NOW = Date.UTC(2026, 9, 20, 18, 30, 5);

  function sample(overrides = {}) {
    const checks = emptyChecks();
    for (const id of CHECK_IDS) setCheck(checks, id, "pass", `${id} ok`);
    const focus = createMeter("focus", 0);
    focus.frames_painted = 100;
    focus.ended_at = 10000;
    return {
      runId: "diag-20261020-183005-7f3a", nowMs: NOW, adapterVersion: "0.1.0",
      environment: { user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15", webcodecs: true, tool_path: "bridge" },
      host: { name: "ChatGPT", version: "1.2026.290", protocol: "2026-01-26", entry: "global", display_mode: "fullscreen", theme: "dark", host_id: HOST_ID },
      session: { host_id: HOST_ID, session_id: SESSION_ID, platform: "android", target_id: "emulator-5584" },
      measurements: { focus: summarizeMeter(focus, 0), thumb: null },
      checks,
      ...overrides,
    };
  }

  test("schema 1 with the run id, versions, host context, measurements and the six checks", () => {
    const results = buildDiagnostics(sample());
    assert.equal(results.schema_version, 1);
    assert.equal(results.run_id, "diag-20261020-183005-7f3a");
    assert.equal(results.created_at, "2026-10-20T18:30:05Z");
    assert.equal(results.page_version, "0.1.0");
    assert.equal(results.adapter_version, "0.1.0");
    assert.equal(results.host_id, HOST_ID);
    assert.deepEqual(results.session, { session_id: SESSION_ID, platform: "android" });
    assert.equal(results.host.entry, "global");
    assert.equal(results.host.name, "ChatGPT");
    assert.equal(results.environment.user_agent.startsWith("Mozilla/5.0"), true, "a user agent is not a path");
    assert.equal(results.measurements.focus.frames_per_s, 10);
    assert.equal(results.measurements.thumb, null);
    assert.deepEqual(results.checks.map((c) => [c.id, c.status]), CHECK_IDS.map((id) => [id, "pass"]));
    assert.deepEqual(results.summary, { pass: 6, fail: 0, not_exercised: 0 });
    assert.equal(results.removed_strings, 0);
    assert.ok(JSON.stringify(results).length < 256 * 1024);
  });

  test("missing and bad checks become not_exercised in the fixed order", () => {
    const results = buildDiagnostics(sample({ checks: { "input.tap": { status: "fail", detail: "control_held" }, "media.focus": { status: "weird" }, extra: { status: "pass" } } }));
    assert.deepEqual(results.checks.map((c) => c.status), ["not_exercised", "not_exercised", "not_exercised", "fail", "not_exercised", "not_exercised"]);
    assert.equal(results.checks[3].detail, "control_held");
    assert.deepEqual(results.summary, { pass: 0, fail: 1, not_exercised: 5 });
    assert.ok(!results.checks.some((c) => c.id === "extra"));
  });

  test("strings that look like tokens or absolute paths are dropped everywhere", () => {
    const checks = emptyChecks();
    setCheck(checks, "input.tap", "fail", "canvas_unavailable: GET http://127.0.0.1:4000/?token=abc");
    setCheck(checks, "control.pause", "pass", "lease held, generation 4");
    const results = buildDiagnostics(sample({
      environment: {
        user_agent: "ok agent", bearer: `Bearer ${TOKEN}`, raw: TOKEN, path: "/Users/someone/Library/x.json",
        home: "/home/u", tilde: "~/secrets", win: "C:\\Users\\x", file: "file:///tmp/a", nested: { list: ["fine", `see /private/var/tmp/x`, "/d/abc/tap"] },
        [`/Users/key`]: 1,
      },
      host: { name: `ChatGPT ${TOKEN}`, version: "1.0", entry: "thread", host_id: "h_bad" },
      checks,
    }));
    const text = JSON.stringify(results);
    for (const needle of [TOKEN, "Bearer", "bearer ", "token=", "/Users/", "/home/", "~/", "C:\\", "file:", "/private/", "/d/abc"]) {
      assert.ok(!text.includes(needle), `kept ${needle}`);
    }
    assert.equal(results.environment.user_agent, "ok agent");
    assert.deepEqual(results.environment.nested.list, ["fine"]);
    assert.equal(results.host.name, undefined);
    assert.equal(results.host.entry, "thread");
    assert.equal(results.host_id, null, "a bad host id is not kept");
    assert.equal(results.checks[3].status, "fail", "a check keeps its status when its detail is dropped");
    assert.equal(results.checks[3].detail, "");
    assert.equal(results.checks[4].detail, "lease held, generation 4");
    assert.ok(results.removed_strings >= 12, String(results.removed_strings));
  });

  test("the server's own refusal rules never match a built result (bearer, token=, HOME)", () => {
    const home = "/Users/tester";
    const results = buildDiagnostics(sample({ environment: { a: `${home}/x`, b: `token=${TOKEN}`, c: "bearer x" } }));
    const text = JSON.stringify(results).toLowerCase();
    assert.ok(!text.includes("bearer "));
    assert.ok(!text.includes("token="));
    assert.ok(!text.includes(home.toLowerCase()));
  });

  test("a bad run id is replaced by a fresh one and odd inputs never throw", () => {
    const results = buildDiagnostics(sample({ runId: "probe-1" }));
    assert.match(results.run_id, DIAG_RUN_ID_PATTERN);
    assert.ok(results.run_id.startsWith("diag-20261020-183005-"));
    const bare = buildDiagnostics();
    assert.equal(bare.schema_version, 1);
    assert.match(bare.run_id, DIAG_RUN_ID_PATTERN);
    assert.equal(bare.checks.length, 6);
    assert.equal(bare.adapter_version, null);
    assert.equal(buildDiagnostics(sample({ adapterVersion: "/tmp/x" })).adapter_version, null);
  });

  test("looksSecretOrPath keeps ordinary values", () => {
    for (const ok of ["avc1.42C01E", "2026-10-20T18:30:05Z", "emulator-5584", "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", HOST_ID,
      "op_0123456789abcdef", "focus completed", "1/2 frames", "https://chatgpt.com", "lease held, generation 4",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"]) {
      assert.equal(looksSecretOrPath(ok), false, ok);
    }
    for (const bad of ["Bearer abc", "bearer.abc", "x token=1", "/Users/a", "/tmp", "/var/folders/x", "~/x", "D:/x", "file:x",
      "abcdefghijklmnopqrstuvwxyz0123456789", "Authorization: x", "access_token", "see /a/b"]) {
      assert.equal(looksSecretOrPath(bad), true, bad);
    }
    assert.equal(looksSecretOrPath(5), false);
  });

  test("dropSecrets limits depth and normalizes numbers", () => {
    let deep = { v: "x" };
    for (let i = 0; i < 20; i++) deep = { deep };
    const { value, removed } = dropSecrets({ deep, n: Number.NaN, f: () => 1, u: undefined, ok: [1, true, null] });
    assert.ok(removed >= 1);
    assert.equal(value.n, null);
    assert.equal("f" in value, false);
    assert.deepEqual(value.ok, [1, true, null]);
  });

  test("makeRunId uses UTC and a 4-hex suffix", () => {
    assert.equal(makeRunId(NOW, () => "ABCD"), "diag-20261020-183005-abcd");
    assert.match(makeRunId(NOW, () => "zz"), DIAG_RUN_ID_PATTERN);
    assert.match(makeRunId(Number.NaN), DIAG_RUN_ID_PATTERN);
  });
});

// ------------------------------------------------------------------------------------------ bridge

describe("BridgeClient", () => {
  function harness(options = {}) {
    const parent = {};
    const posted = [];
    let listener = null;
    const bridge = new BridgeClient({ post: (m) => posted.push(m), addListener: (h) => { listener = h; }, parent, ...options });
    return { bridge, posted, parent, deliver: (data, source = parent) => listener({ source, data }) };
  }

  test("requests resolve and reject by id; foreign sources are ignored", async () => {
    const { bridge, posted, deliver } = harness();
    const one = bridge.request("tools/call", { name: "list_sessions", arguments: {} });
    const two = bridge.request("tools/call", { name: "x" });
    assert.deepEqual(posted[0], { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "list_sessions", arguments: {} } });
    deliver({ jsonrpc: "2.0", id: 1, result: { ok: 1 } }, {});
    assert.equal(bridge.pendingCount, 2, "a message from another window is ignored");
    deliver({ jsonrpc: "2.0", id: 2, error: { code: -32602, message: "Unknown tool" } });
    deliver({ jsonrpc: "2.0", id: 1, result: { ok: 1 } });
    assert.deepEqual(await one, { ok: 1 });
    await assert.rejects(two, (error) => error instanceof BridgeError && error.code === "bridge_error" && /Unknown tool/.test(error.message));
    assert.equal(bridge.pendingCount, 0);
  });

  test("timeouts, notifications, requests from the host and close", async () => {
    let fire = null;
    const { bridge, posted, deliver } = harness({ setTimer: (fn) => { fire = fn; return 1; }, clearTimer: () => {} });
    const pending = bridge.request("ui/initialize", {});
    fire();
    await assert.rejects(pending, (error) => error.code === "bridge_timeout");
    const seen = [];
    bridge.onNotification("ui/notifications/tool-result", (p) => seen.push(p));
    bridge.onNotification("ui/notifications/tool-result", () => { throw new Error("view bug"); });
    deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { a: 1 } });
    assert.deepEqual(seen, [{ a: 1 }]);
    deliver({ jsonrpc: "2.0", id: "t1", method: "ui/resource-teardown", params: {} });
    deliver({ jsonrpc: "2.0", id: "t2", method: "ui/unknown" });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(posted.find((m) => m.id === "t1"), { jsonrpc: "2.0", id: "t1", result: {} });
    assert.equal(posted.find((m) => m.id === "t2").error.code, -32601);
    assert.equal(deliver({ jsonrpc: "1.0", id: 1 }), false);
    bridge.notify("ui/notifications/initialized", {});
    assert.deepEqual(posted.at(-1), { jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    const left = bridge.request("tools/call", {});
    bridge.close();
    await assert.rejects(left, /bridge closed/);
    await assert.rejects(bridge.request("tools/call", {}), /Bridge closed/);
  });

  test("a failing post rejects at once", async () => {
    const bridge = new BridgeClient({ post: () => { throw new Error("gone"); }, parent: {} });
    await assert.rejects(bridge.request("tools/call", {}), (error) => error.code === "bridge_error" && /gone/.test(error.message));
    assert.equal(bridge.pendingCount, 0);
    assert.throws(() => new BridgeClient({}), TypeError);
  });
});

// ------------------------------------------------------------------------------------------ page run

class FakeElement {
  constructor(id, tag = "div") {
    this.id = id;
    this.tagName = tag.toUpperCase();
    this.textContent = "";
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.width = 0;
    this.height = 0;
    this.className = "";
    this.drawn = 0;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn(event); }
  replaceChildren() { this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getBoundingClientRect() { return { left: 0, top: 0, width: 108, height: 240 }; }
  getContext() { return { drawImage: () => { this.drawn += 1; } }; }
}

function fakeDocument() {
  const ids = [...PAGE.matchAll(/\bid="([a-z0-9-]+)"/g)].map((m) => m[1]);
  const elements = new Map(ids.map((id) => [id, new FakeElement(id, id === "screen" ? "canvas" : "div")]));
  elements.get("workspace-config").textContent = JSON.stringify({ adapter_version: "0.1.0", schema_version: "autonom.mcp/v1" });
  return {
    elements,
    documentElement: { dataset: {} },
    getElementById: (id) => elements.get(id) || null,
    createElement: (tag) => new FakeElement(null, tag),
  };
}

const SESSION_B = "s_bbbbbbbbbb";
const STREAM_B = "st_bbbbbbbbbbbbbbbb";

/**
 * A fake ChatGPT host: answers the bridge, scripts the tools and records every call. `sessions` replaces
 * the listed sessions and `delays` holds per-tool answer delays in milliseconds.
 */
function fakeHost({ sessions = null, delays = {} } = {}) {
  const calls = [];
  const timers = new Set();
  let listener = null;
  let seq = 0;
  let thumbSeq = 100;
  let tapRefusals = 1;
  const host = {
    calls,
    stop() { for (const t of timers) clearTimeout(t); },
  };
  const later = (fn, ms = 2) => {
    const t = setTimeout(() => { timers.delete(t); fn(); }, ms);
    timers.add(t);
  };
  const answer = (id, result, ms = 2) => later(() => listener({ source: parent, data: { jsonrpc: "2.0", id, result } }), ms);
  const defaultSessions = [{ host_id: HOST_ID, session_id: SESSION_ID, platform: "android", target_id: "emulator-5584", label: "Pixel", live: true }];
  const tools = {
    list_hosts: () => envelope("list_hosts", { hosts: [{ host_id: HOST_ID, kind: "local", name: "mac", state: "online" }] }),
    list_sessions: () => envelope("list_sessions", { sessions: sessions || defaultSessions }),
    open_stream: (args) => {
      const session = { host_id: HOST_ID, session_id: args.session_id };
      const streamId = args.session_id === SESSION_B ? STREAM_B : args.profile === "thumb" ? "st_00000000000000ff" : STREAM_ID;
      return envelope("open_stream", { session, stream: streamObj({ profile: args.profile, stream_id: streamId, session }) });
    },
    get_frames: (args) => {
      if (args.stream_id === STREAM_ID) {
        if (seq >= 24) return framesResult([], { config: null });
        const frames = [];
        for (let i = 0; i < 6; i++) {
          seq += 1;
          frames.push([seq, seq === 1, seq === 1 ? IDR : DELTA]);
        }
        return framesResult(frames, { config: seq <= 6 ? CONFIG : null });
      }
      thumbSeq += 30;
      return framesResult([[thumbSeq, true, IDR]], { stream: { profile: "thumb" } });
    },
    close_stream: () => envelope("close_stream", { closed: true }),
    send_input: (args) => {
      if (args.event.op === "tap" && tapRefusals > 0) {
        tapRefusals -= 1;
        return failure("send_input", "control_held", { session: { host_id: HOST_ID, session_id: SESSION_ID }, control: { state: "held", role: "agent", generation: 3, mine: true } });
      }
      return envelope("send_input", { session: { host_id: HOST_ID, session_id: SESSION_ID }, input: { op: args.event.op, operation_id: "op_0123456789abcdef" } });
    },
    pause_session: () => envelope("pause_session", { control: { state: "held", role: "human", generation: 4, mine: true } }),
    resume_session: () => envelope("resume_session", { control: { state: "free", role: null, generation: 4, mine: false } }),
    save_diagnostics: (args) => envelope("save_diagnostics", { saved: { run_id: args.results.run_id, bytes: JSON.stringify(args.results).length + 1 } }),
  };
  const parent = {
    postMessage(message) {
      if (!message || message.jsonrpc !== "2.0" || message.id === undefined) return;
      if (message.method === "ui/initialize") {
        answer(message.id, { protocolVersion: "2026-01-26", hostInfo: { name: "ChatGPT", version: "test" }, hostContext: { theme: "dark", entrypoint: "global", displayMode: "inline" } });
        return;
      }
      if (message.method === "tools/call") {
        calls.push({ name: message.params.name, arguments: message.params.arguments });
        const tool = tools[message.params.name];
        answer(message.id, tool ? tool(message.params.arguments) : failure(message.params.name, "unknown_tool"), delays[message.params.name] ?? 2);
      }
    },
  };
  host.parent = parent;
  host.window = {
    parent,
    addEventListener: (type, handler) => { if (type === "message") listener = handler; },
    performance: { now: () => performance.now() },
    navigator: { userAgent: "FakeAgent 1.0", language: "en" },
    devicePixelRatio: 2,
    setTimeout: (fn, ms) => { const t = setTimeout(() => { timers.delete(t); fn(); }, Math.min(ms, 20)); timers.add(t); return t; },
    setInterval: (fn, ms) => { const t = setInterval(fn, ms); t.unref(); return t; },
    clearInterval: (t) => clearInterval(t),
  };
  host.decoded = [];
  const decoded = host.decoded;
  class FakeVideoDecoder {
    static async isConfigSupported(config) { return { supported: config.codec === "avc1.42C01E" }; }
    constructor({ output }) { this.output = output; this.state = "unconfigured"; this.decodeQueueSize = 0; }
    configure(config) { this.config = config; this.state = "configured"; }
    decode(chunk) {
      decoded.push(chunk);
      const out = this.output;
      queueMicrotask(() => out({ timestamp: chunk.timestamp, displayWidth: 1080, displayHeight: 2400, close() {} }));
    }
    async flush() {}
    close() { this.state = "closed"; }
  }
  class FakeChunk { constructor(init) { Object.assign(this, init); } }
  host.window.VideoDecoder = FakeVideoDecoder;
  host.window.EncodedVideoChunk = FakeChunk;
  host.window.window = host.window;
  return host;
}

async function waitFor(predicate, what, timeoutMs = 4000) {
  const end = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

describe("the page against a fake host bridge", () => {
  test("lists sessions, decodes focus and thumb, taps, pauses, resumes and saves diagnostics", async () => {
    const doc = fakeDocument();
    const host = fakeHost();
    const page = startPage(doc, host.window);
    try {
      await page.booted;
      const S = page.state;
      const el = (id) => doc.elements.get(id);
      assert.equal(S.toolPath, "bridge");
      assert.equal(doc.documentElement.dataset.theme, "dark");
      assert.equal(el("adapter-version").textContent, "0.1.0");
      assert.equal(el("host-id").textContent, HOST_ID);
      assert.equal(el("entry").textContent, "Sidebar (global)");
      assert.equal(S.checks["bridge.list_sessions"].status, "pass");
      assert.equal(S.session.session_id, SESSION_ID, "the only session is chosen");
      assert.equal(el("start-focus").disabled, false);

      el("start-focus").dispatch("click");
      await waitFor(() => S.checks["media.focus"].status === "pass", "focus frames");
      const pulls = host.calls.filter((c) => c.name === "get_frames");
      assert.deepEqual(pulls[0].arguments, { stream_id: STREAM_ID, after_seq: 0, wait_ms: 250 });
      assert.deepEqual(pulls[1].arguments, { stream_id: STREAM_ID, after_seq: 6, wait_ms: 250, epoch: 1 });
      assert.equal(host.decoded[0].type, "key");
      assert.deepEqual([...host.decoded[0].data], [...CONFIG, ...IDR], "the config precedes the key frame");
      assert.equal(el("screen").width, 1080);
      assert.ok(el("screen").drawn >= 10);

      // A tap while the agent holds the lease shows the hint and does not fail the check; after Pause it passes.
      el("screen").dispatch("click", { clientX: 54, clientY: 120 });
      await waitFor(() => el("notice").textContent === AGENT_HAS_CONTROL, "the agent-control hint");
      assert.equal(S.checks["input.tap"].status, "not_exercised");
      assert.equal(host.calls.filter((c) => c.name === "send_input").length, 1, "a refused tap is not retried");
      el("pause").dispatch("click");
      await waitFor(() => S.checks["control.pause"].status === "pass", "pause");
      el("screen").dispatch("click", { clientX: 54, clientY: 120 });
      await waitFor(() => S.checks["input.tap"].status === "pass", "tap");
      const taps = host.calls.filter((c) => c.name === "send_input");
      assert.deepEqual(taps[1].arguments, { host_id: HOST_ID, session_id: SESSION_ID, event: { op: "tap", x: 0.5, y: 0.5 } });
      el("home").dispatch("click");
      el("back").dispatch("click");
      await waitFor(() => host.calls.filter((c) => c.name === "send_input").length === 4, "home and back");
      assert.deepEqual(host.calls.filter((c) => c.name === "send_input").slice(2).map((c) => c.arguments.event),
        [{ op: "system", button: "home" }, { op: "system", button: "back" }]);
      el("resume").dispatch("click");
      await waitFor(() => S.checks["control.resume"].status === "pass", "resume");

      el("start-thumb").dispatch("click");
      await waitFor(() => S.checks["media.thumb"].status === "pass", "thumb frames");
      assert.ok(host.calls.some((c) => c.name === "close_stream" && c.arguments.stream_id === STREAM_ID), "the focus stream was closed");
      const thumbPull = host.calls.filter((c) => c.name === "get_frames" && c.arguments.stream_id !== STREAM_ID)[0];
      assert.equal(thumbPull.arguments.wait_ms, 1000);
      el("stop-stream").dispatch("click");
      await waitFor(() => host.calls.some((c) => c.name === "close_stream" && c.arguments.stream_id === "st_00000000000000ff"), "thumb close");

      el("save").dispatch("click");
      await waitFor(() => /^diag-/.test(el("saved").textContent), "save");
      const results = host.calls.find((c) => c.name === "save_diagnostics").arguments.results;
      assert.equal(results.schema_version, 1);
      assert.match(results.run_id, DIAG_RUN_ID_PATTERN);
      assert.deepEqual(results.checks.map((c) => [c.id, c.status]), CHECK_IDS.map((id) => [id, "pass"]));
      assert.equal(results.host_id, HOST_ID);
      assert.equal(results.host.entry, "global");
      assert.equal(results.host.name, "ChatGPT");
      assert.equal(results.environment.tool_path, "bridge");
      assert.equal(results.measurements.focus.frames_painted >= 10, true);
      assert.equal(results.measurements.focus.codec, "avc1.42C01E");
      assert.equal(results.measurements.thumb.key_frames >= 2, true);
      assert.ok(results.measurements.focus.call_ms_median !== null);
      assert.match(el("saved").textContent, /^diag-/);
      const allowed = new Set(["list_hosts", "list_sessions", "open_stream", "get_frames", "close_stream", "send_input",
        "pause_session", "resume_session", "save_diagnostics"]);
      for (const call of host.calls) assert.ok(allowed.has(call.name), call.name);
    } finally {
      page.stop();
      host.stop();
    }
  });

  const TWO_SESSIONS = [
    { host_id: HOST_ID, session_id: SESSION_ID, platform: "android", target_id: "emulator-5584", label: "Pixel", live: true },
    { host_id: HOST_ID, session_id: SESSION_B, platform: "ios", target_id: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", label: "iPhone", live: true },
  ];
  const settle = () => new Promise((resolve) => setTimeout(resolve, 120));
  const count = (host, name, pred = () => true) => host.calls.filter((c) => c.name === name && pred(c.arguments)).length;

  test("choosing another session while open_stream is in flight drops and closes the old stream", async () => {
    const doc = fakeDocument();
    const host = fakeHost({ sessions: TWO_SESSIONS, delays: { open_stream: 40 } });
    const page = startPage(doc, host.window);
    try {
      await page.booted;
      const S = page.state;
      const el = (id) => doc.elements.get(id);
      assert.equal(S.session, null, "two sessions: none is chosen");
      el("sessions").children[0].children[0].dispatch("click");
      await waitFor(() => S.session && S.session.session_id === SESSION_ID, "session A chosen");
      el("start-focus").dispatch("click");
      await waitFor(() => count(host, "open_stream") === 1, "open_stream sent");
      el("sessions").children[1].children[0].dispatch("click");
      await waitFor(() => count(host, "close_stream", (a) => a.stream_id === STREAM_ID) === 1, "A's late stream closed");
      await settle();
      assert.equal(S.session.session_id, SESSION_B);
      assert.equal(S.stream, null, "no stream of session A is kept for session B");
      assert.equal(S.loop, null);
      assert.equal(count(host, "get_frames"), 0, "nothing is pulled for the dropped stream");
      el("screen").width = 1080;
      el("screen").height = 2400;
      el("screen").dispatch("click", { clientX: 54, clientY: 120 });
      await settle();
      assert.equal(count(host, "send_input"), 0, "no tap goes out without a stream of the chosen session");

      // A stream started for B belongs to B, and taps go to B.
      el("start-focus").dispatch("click");
      await waitFor(() => S.stream && S.stream.stream_id === STREAM_B, "B's stream");
      assert.equal(host.calls.filter((c) => c.name === "open_stream").at(-1).arguments.session_id, SESSION_B);
      el("screen").dispatch("click", { clientX: 54, clientY: 120 });
      await waitFor(() => count(host, "send_input") === 1, "tap");
      assert.equal(host.calls.find((c) => c.name === "send_input").arguments.session_id, SESSION_B);
    } finally {
      page.stop();
      host.stop();
    }
  });

  test("two quick Start clicks open one stream; Stop during an open closes it and pulls nothing", async () => {
    const doc = fakeDocument();
    const host = fakeHost({ delays: { open_stream: 40 } });
    const page = startPage(doc, host.window);
    try {
      await page.booted;
      const S = page.state;
      const el = (id) => doc.elements.get(id);
      el("start-focus").dispatch("click");
      el("start-focus").dispatch("click");
      await waitFor(() => S.stream && S.stream.stream_id === STREAM_ID, "one stream");
      await settle();
      assert.equal(count(host, "open_stream"), 1, "the first of two Start clicks in one turn is superseded");
      assert.equal(count(host, "close_stream"), 0);
      // A Start while the open is in flight closes the stream that answers late.
      el("start-thumb").dispatch("click");
      await waitFor(() => count(host, "open_stream") === 2, "thumb open sent");
      el("start-focus").dispatch("click");
      await waitFor(() => count(host, "open_stream") === 3, "focus open sent");
      await waitFor(() => S.stream && S.stream.profile === "focus" && count(host, "close_stream") >= 2, "late thumb closed");
      await settle();
      assert.equal(S.stream.stream_id, STREAM_ID);
      assert.equal(count(host, "close_stream", (a) => a.stream_id === "st_00000000000000ff"), 1, "the superseded thumb stream is closed");
      // Stop while open_stream is in flight: the late answer is closed and never pulled.
      el("stop-stream").dispatch("click");
      await waitFor(() => S.stream === null, "stopped");
      await settle();
      const opens = count(host, "open_stream");
      const closes = count(host, "close_stream");
      el("start-focus").dispatch("click");
      await waitFor(() => count(host, "open_stream") === opens + 1, "open sent");
      el("stop-stream").dispatch("click");
      await waitFor(() => count(host, "close_stream") === closes + 1, "late stream closed");
      const pullsAfterStop = count(host, "get_frames");
      await settle();
      assert.equal(S.stream, null);
      assert.equal(S.loop, null);
      assert.equal(count(host, "get_frames"), pullsAfterStop, "no frames are pulled after Stop");
      assert.equal(count(host, "open_stream"), count(host, "close_stream"), "every opened stream was closed");
    } finally {
      page.stop();
      host.stop();
    }
  });

  test("outside a host the page says so and calls nothing", async () => {
    const doc = fakeDocument();
    const host = fakeHost();
    const win = { ...host.window, parent: undefined };
    win.window = win;
    const page = startPage(doc, win);
    try {
      await page.booted;
      assert.equal(page.state.toolPath, null);
      assert.match(doc.elements.get("notice").textContent, /Not running inside an MCP host/);
      assert.equal(host.calls.length, 0);
      assert.equal(page.state.checks["bridge.list_sessions"].status, "not_exercised");
    } finally {
      page.stop();
      host.stop();
    }
  });
});
