import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import * as http2 from "node:http2";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";

import {
  DEFAULT_VIDEO_OPTIONS,
  FRAME_QUEUE_HIGH_WATER,
  ERROR_CODE,
  GrpcError,
  GrpcMessageParser,
  HID_BUTTON,
  IDB_CAPABILITY,
  IDB_COMPANION_BIN_ENV,
  IDB_COMPANION_HINT,
  IDB_INSTALL_HINT,
  IdbCompanion,
  IdbCompanionClient,
  IdbVideoStream,
  StructuredError,
  VIDEO_FORMAT,
  analyzeAccessUnit,
  decodeHidEvent,
  decodeOrientationResponse,
  decodeProtoFields,
  decodeTargetDescriptionResponse,
  decodeVideoStreamRequest,
  decodeVideoStreamResponse,
  encodeHidEvent,
  encodeVideoStreamResponse,
  encodeVideoStreamStart,
  encodeVideoStreamStop,
  freeLoopbackPort,
  grpcFrame,
  idbUnavailableError,
  normalizeVideoOptions,
  parseSps,
  reapAllCompanionsSync,
  resolveIdbCompanionBinary,
  splitNalUnits,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";

const MODULE_URL = new URL(
  "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs",
  import.meta.url,
).href;
const UDID = "3760A5A8-E59D-4AE6-B1AF-A626908D7B61";

// Golden bytes from the reference protobuf library (fb-idb idb_pb2, SerializeToString).
const GOLDEN = {
  start: "0a1d103c21000000000000e03f29000000000000f03f390000000000000040",
  startFull: "0a28101e1802219a9999999999e93f29000000000000e83f310000000080845e4139000000000000f83f",
  stop: "1200",
  respPayload: "12081206000000016588",
  respLog: "0a026869",
  touchDown: "0a180a160a140a12090000000000006940110000000000548440",
  touchUp: "0a110a0d0a0b0a091100000000008028401001",
  homeDown: "0a060a0412020801",
  lockUp: "0a080a04120208021001",
  keyDown: "0a060a041a020804",
  keyUpBig: "0a090a051a0308ac021001",
  describe:
    "0a660a0341424312066950686f6e651a1508b60910be1419000000000000084020920328ea062206426f6f7465642a0973696d756c61746f723208694f532032372e303a0561726d36345a1c0a02753112034c43441801200128b60930be1439000000000000084012070a034142432001",
  orientation: "0803",
};

// SPS and PPS of a real iPhone 17 simulator stream (idb_companion 1.6.4, 1206x2622).
const REAL_SPS = Buffer.from("27640033ac13143c04c0149e6a9a80868083c20108f8", "hex");
const REAL_PPS = Buffer.from("28ee3cb0", "hex");
const SC4 = Buffer.from([0, 0, 0, 1]);
const SC3 = Buffer.from([0, 0, 1]);
const KEY_FRAME = Buffer.concat([SC4, REAL_SPS, SC4, REAL_PPS, SC4, Buffer.from([0x65, 0x88, 0x80, 0x10, 0x00, 0x01])]);
const P_FRAME = Buffer.concat([SC4, Buffer.from([0x41, 0x9a, 0x02, 0x00])]);

const hex = (buf) => Buffer.from(buf).toString("hex");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(check, { timeoutMs = 3000, stepMs = 10, message = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return;
    await sleep(stepMs);
  }
  assert.fail(`Timed out waiting for ${message}.`);
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

function tempDir(t, prefix = "idbt-") {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

// ---------------------------------------------------------------------------
// Protobuf
// ---------------------------------------------------------------------------

test("Start and Stop encode exactly like the reference library", () => {
  assert.equal(hex(encodeVideoStreamStart()), GOLDEN.start);
  assert.deepEqual(DEFAULT_VIDEO_OPTIONS, { fps: 60, format: "h264", compressionQuality: 0.5, scaleFactor: 1, keyFrameRate: 2 });
  assert.equal(
    hex(
      encodeVideoStreamStart({
        fps: 30,
        format: "mjpeg",
        compressionQuality: 0.8,
        scaleFactor: 0.75,
        avgBitrate: 8_000_000,
        keyFrameRate: 1.5,
      }),
    ),
    GOLDEN.startFull,
  );
  assert.equal(hex(encodeVideoStreamStop()), GOLDEN.stop);
});

test("HID presses encode exactly like the reference library", () => {
  assert.equal(hex(encodeHidEvent({ touch: { x: 200, y: 650.5 }, direction: "down" })), GOLDEN.touchDown);
  assert.equal(hex(encodeHidEvent({ touch: { x: 0, y: 12.25 }, direction: "up" })), GOLDEN.touchUp);
  assert.equal(hex(encodeHidEvent({ button: "HOME", direction: "down" })), GOLDEN.homeDown);
  assert.equal(hex(encodeHidEvent({ button: HID_BUTTON.LOCK, direction: "UP" })), GOLDEN.lockUp);
  assert.equal(hex(encodeHidEvent({ key: 4, direction: 0 })), GOLDEN.keyDown);
  assert.equal(hex(encodeHidEvent({ key: 300, direction: 1 })), GOLDEN.keyUpBig);
});

test("responses from the reference library decode", () => {
  assert.deepEqual(decodeVideoStreamResponse(Buffer.from(GOLDEN.respPayload, "hex")), {
    data: Buffer.from([0, 0, 0, 1, 0x65, 0x88]),
    log: null,
  });
  assert.deepEqual(decodeVideoStreamResponse(Buffer.from(GOLDEN.respLog, "hex")), { data: null, log: Buffer.from("hi") });
  assert.deepEqual(decodeVideoStreamResponse(Buffer.alloc(0)), { data: null, log: null });
  assert.deepEqual(decodeTargetDescriptionResponse(Buffer.from(GOLDEN.describe, "hex")), {
    udid: "ABC",
    name: "iPhone",
    screen: { width: 1206, height: 2622, density: 3, widthPoints: 402, heightPoints: 874 },
    state: "Booted",
    targetType: "simulator",
    osVersion: "iOS 27.0",
    architecture: "arm64",
    displays: [{ uniqueId: "u1", name: "LCD", active: true, integrated: true, width: 1206, height: 2622, density: 3 }],
  });
  assert.equal(decodeOrientationResponse(Buffer.from(GOLDEN.orientation, "hex")), "LANDSCAPE_LEFT");
  assert.equal(decodeOrientationResponse(Buffer.alloc(0)), "UNKNOWN");
  assert.equal(decodeOrientationResponse(Buffer.from([0x08, 0x63])), "UNKNOWN");
});

test("requests and HID events round-trip through encode and decode", () => {
  assert.deepEqual(decodeVideoStreamRequest(encodeVideoStreamStart({ fps: 0, avgBitrate: 4e6, keyFrameRate: 0 })), {
    type: "start",
    filePath: "",
    fps: 0,
    format: VIDEO_FORMAT.H264,
    compressionQuality: 0.5,
    scaleFactor: 1,
    avgBitrate: 4e6,
    keyFrameRate: 0,
  });
  assert.deepEqual(decodeVideoStreamRequest(encodeVideoStreamStop()), { type: "stop" });
  // oneof: the last choice on the wire wins.
  const both = Buffer.concat([encodeVideoStreamStop(), encodeVideoStreamStart()]);
  assert.equal(decodeVideoStreamRequest(both).type, "start");
  assert.equal(decodeVideoStreamRequest(Buffer.concat([encodeVideoStreamStart(), encodeVideoStreamStop()])).type, "stop");
  assert.deepEqual(decodeVideoStreamRequest(Buffer.alloc(0)), { type: "unknown" });

  for (const event of [
    { touch: { x: 0, y: 0 }, direction: "down" },
    { touch: { x: 401.5, y: 873.25 }, direction: "up" },
    { button: "HOME", direction: "down" },
    { button: "LOCK", direction: "up" },
    { key: 0, direction: "down" },
    { key: 2 ** 40, direction: "up" },
  ]) {
    assert.deepEqual(decodeHidEvent(encodeHidEvent(event)), event);
  }
  assert.equal(decodeHidEvent(Buffer.alloc(0)), null);

  const big = Buffer.alloc(300_000, 7);
  assert.deepEqual(decodeVideoStreamResponse(encodeVideoStreamResponse({ data: big })).data, big);
  assert.deepEqual(decodeVideoStreamResponse(encodeVideoStreamResponse({ log: "x" })).log, Buffer.from("x"));
});

// Golden bytes from the reference protobuf library (fb-idb idb_pb2): HIDEvent(pinch=HIDPinch(...)).
const PINCH_GOLDEN = [
  [{ center: { x: 201, y: 437 }, scale: 2, duration: 0.6, radius: 60 },
    "222f0a12090000000000206940110000000000507b4011000000000000004019333333333333e33f210000000000004e40"],
  // Zero center.x and duration are proto3 defaults and left out, as the reference library does.
  [{ center: { x: 0, y: 437.5 }, scale: 0.5, duration: 0, radius: 12.25 },
    "221d0a09110000000000587b4011000000000000e03f210000000000802840"],
];

test("a pinch is HIDEvent field 4 with center, scale, duration and radius, byte for byte as the reference library writes it", () => {
  for (const [pinch, golden] of PINCH_GOLDEN) {
    const bytes = encodeHidEvent({ pinch });
    assert.equal(hex(bytes), golden);
    // Field by field: HIDEvent.pinch = 4 (length-delimited), HIDPinch center = 1 (Point x = 1,
    // y = 2), scale = 2, duration = 3, radius = 4 (doubles).
    const [event] = decodeProtoFields(bytes);
    assert.deepEqual([event.field, event.wire], [4, 2]);
    const fields = new Map(decodeProtoFields(event.value).map((record) => [record.field, record]));
    const center = new Map(decodeProtoFields(fields.get(1).value).map((record) => [record.field, record.value.readDoubleLE(0)]));
    assert.equal(center.get(1) ?? 0, pinch.center.x);
    assert.equal(center.get(2), pinch.center.y);
    assert.equal(fields.get(2).value.readDoubleLE(0), pinch.scale);
    assert.equal(fields.get(3)?.value.readDoubleLE(0) ?? 0, pinch.duration);
    assert.equal(fields.get(4).value.readDoubleLE(0), pinch.radius);
    assert.deepEqual(decodeHidEvent(bytes), { pinch });
  }
  for (const pinch of [
    { center: { x: -1, y: 1 }, scale: 1, duration: 0.2, radius: 10 },
    { center: { x: 1, y: Number.NaN }, scale: 1, duration: 0.2, radius: 10 },
    { center: { x: 1, y: 1 }, scale: 0, duration: 0.2, radius: 10 },
    { center: { x: 1, y: 1 }, scale: 1, duration: -0.1, radius: 10 },
    { center: { x: 1, y: 1 }, scale: 1, duration: 0.2, radius: 0 },
    { scale: 1, duration: 0.2, radius: 10 },
  ]) {
    assert.throws(() => encodeHidEvent({ pinch }), /pinch/, JSON.stringify(pinch));
  }
});

test("the protobuf decoder skips unknown fields and rejects broken input", () => {
  const extra = Buffer.concat([
    Buffer.from(GOLDEN.orientation, "hex"),
    Buffer.from([0x10, 0xff, 0x01]), // field 2 varint
    Buffer.from([0x1d, 1, 2, 3, 4]), // field 3 fixed32
    Buffer.from([0x21, 1, 2, 3, 4, 5, 6, 7, 8]), // field 4 fixed64
    Buffer.from([0x2a, 0x02, 0xaa, 0xbb]), // field 5 bytes
  ]);
  assert.equal(decodeOrientationResponse(extra), "LANDSCAPE_LEFT");
  assert.equal(decodeProtoFields(extra).length, 5);
  assert.throws(() => decodeProtoFields(Buffer.from([0x08, 0x80])), /Truncated varint/);
  assert.throws(() => decodeProtoFields(Buffer.from([0x0a, 0x05, 0x01])), /Truncated length-delimited/);
  assert.throws(() => decodeProtoFields(Buffer.from([0x09, 0x01])), /Truncated 64-bit/);
  assert.throws(() => decodeProtoFields(Buffer.from([0x0d, 0x01])), /Truncated 32-bit/);
  assert.throws(() => decodeProtoFields(Buffer.from([0x0b])), /wire type 3/);
  assert.throws(() => decodeProtoFields(Buffer.from([0x00, 0x00])), /field number 0/);
  assert.throws(() => decodeProtoFields(Buffer.from([0x08, ...Array(10).fill(0xff), 0x01])), /longer than 10 bytes|outside uint64/);
});

test("invalid Start options and HID events are refused", () => {
  assert.throws(() => normalizeVideoOptions({ fps: 1.5 }), RangeError);
  assert.throws(() => normalizeVideoOptions({ fps: 241 }), RangeError);
  assert.throws(() => normalizeVideoOptions({ scaleFactor: 0 }), RangeError);
  assert.throws(() => normalizeVideoOptions({ scaleFactor: 1.5 }), RangeError);
  assert.throws(() => normalizeVideoOptions({ compressionQuality: 2 }), RangeError);
  assert.throws(() => normalizeVideoOptions({ compressionQuality: Number.NaN }), TypeError);
  assert.throws(() => normalizeVideoOptions({ avgBitrate: 0 }), RangeError);
  assert.throws(() => normalizeVideoOptions({ format: "vp9" }), RangeError);
  assert.throws(() => normalizeVideoOptions({ format: 42 }), RangeError);
  assert.equal(normalizeVideoOptions({ fps: null }).fps, undefined);
  assert.equal(normalizeVideoOptions({ format: VIDEO_FORMAT.MJPEG }).format, 2);
  assert.throws(() => encodeHidEvent({ touch: { x: -1, y: 0 }, direction: "down" }), RangeError);
  assert.throws(() => encodeHidEvent({ touch: { x: 1, y: Infinity }, direction: "down" }), TypeError);
  assert.throws(() => encodeHidEvent({ touch: { x: 1, y: 1 }, direction: "sideways" }), RangeError);
  assert.throws(() => encodeHidEvent({ button: "BACK", direction: "down" }), RangeError);
  assert.throws(() => encodeHidEvent({ key: -1, direction: "down" }), RangeError);
  assert.throws(() => encodeHidEvent({ direction: "down" }), TypeError);
  assert.throws(() => encodeHidEvent(null), TypeError);
});

// ---------------------------------------------------------------------------
// gRPC framing
// ---------------------------------------------------------------------------

test("gRPC frames reassemble across any chunk boundaries", () => {
  const messages = [Buffer.from("hello"), Buffer.alloc(0), Buffer.alloc(70_000, 3), Buffer.from([1])];
  const wire = Buffer.concat(messages.map(grpcFrame));
  assert.equal(hex(grpcFrame(Buffer.from("hi"))), "00000000026869");

  const oneShot = new GrpcMessageParser().push(wire);
  assert.deepEqual(oneShot, messages);

  const parser = new GrpcMessageParser();
  const byByte = [];
  for (let i = 0; i < wire.length; i += 1) byByte.push(...parser.push(wire.subarray(i, i + 1)));
  assert.deepEqual(byByte, messages);
  assert.equal(parser.pendingBytes, 0);

  const odd = new GrpcMessageParser();
  const out = [];
  for (let i = 0; i < wire.length; i += 4093) out.push(...odd.push(wire.subarray(i, i + 4093)));
  assert.deepEqual(out, messages);
});

test("gRPC framing refuses compressed and oversized messages", () => {
  assert.throws(
    () => new GrpcMessageParser().push(Buffer.from([1, 0, 0, 0, 1, 9])),
    (error) => error instanceof GrpcError && error.code === 13,
  );
  assert.throws(
    () => new GrpcMessageParser({ maxMessageBytes: 10 }).push(Buffer.from([0, 0, 0, 0, 11])),
    (error) => error instanceof GrpcError && error.code === 8,
  );
});

// ---------------------------------------------------------------------------
// H.264
// ---------------------------------------------------------------------------

class BitWriter {
  bits = [];

  u(n, value) {
    for (let i = n - 1; i >= 0; i -= 1) this.bits.push(Math.floor(value / 2 ** i) % 2);
    return this;
  }

  ue(value) {
    const code = value + 1;
    const length = Math.floor(Math.log2(code));
    this.u(length, 0);
    return this.u(length + 1, code);
  }

  se(value) {
    return this.ue(value > 0 ? 2 * value - 1 : -2 * value);
  }

  rbsp() {
    const bits = [...this.bits, 1];
    while (bits.length % 8) bits.push(0);
    const out = [];
    for (let i = 0; i < bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8).join(""), 2));
    return out;
  }
}

function withEmulationPrevention(bytes) {
  const out = [];
  let zeros = 0;
  for (const byte of bytes) {
    if (zeros >= 2 && byte <= 3) {
      out.push(3);
      zeros = 0;
    }
    out.push(byte);
    zeros = byte === 0 ? zeros + 1 : 0;
  }
  return out;
}

function makeSps({
  profile = 66,
  constraint = 0xc0,
  level = 30,
  chroma = 1,
  separate = 0,
  scaling = false,
  pocType = 0,
  pocOffset = 0,
  pocCycle = [],
  widthMbs,
  heightMapUnits,
  frameMbsOnly = 1,
  crop = null,
  vui = false,
}) {
  const w = new BitWriter().ue(0);
  if ([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135].includes(profile)) {
    w.ue(chroma);
    if (chroma === 3) w.u(1, separate);
    w.ue(0).ue(0).u(1, 0);
    w.u(1, scaling ? 1 : 0);
    if (scaling) {
      const lists = chroma === 3 ? 12 : 8;
      for (let i = 0; i < lists; i += 1) {
        const present = i === 0 || i === 6;
        w.u(1, present ? 1 : 0);
        if (!present) continue;
        const size = i < 6 ? 16 : 64;
        // Deltas that walk the scale up, then one delta to 0 ends the list early.
        for (let j = 0; j < size; j += 1) {
          if (j === size - 3) {
            w.se(-(8 + j));
            break;
          }
          w.se(1);
        }
      }
    }
  }
  w.ue(0).ue(pocType);
  if (pocType === 0) w.ue(2);
  else if (pocType === 1) {
    w.u(1, 0).se(pocOffset).se(0).ue(pocCycle.length);
    for (const value of pocCycle) w.se(value);
  }
  w.ue(1).u(1, 0).ue(widthMbs - 1).ue(heightMapUnits - 1).u(1, frameMbsOnly);
  if (!frameMbsOnly) w.u(1, 0);
  w.u(1, 1);
  w.u(1, crop ? 1 : 0);
  if (crop) w.ue(crop.left).ue(crop.right).ue(crop.top).ue(crop.bottom);
  w.u(1, vui ? 1 : 0);
  if (vui) w.u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0);
  return Buffer.from([0x67, ...withEmulationPrevention([profile, constraint, level, ...w.rbsp()])]);
}

test("the real simulator SPS gives 1206x2622 after cropping and avc1.640033", () => {
  assert.deepEqual(parseSps(REAL_SPS), {
    profileIdc: 100,
    constraintFlags: 0,
    levelIdc: 51,
    chromaFormatIdc: 1,
    frameMbsOnly: true,
    width: 1206,
    height: 2622,
    codec: "avc1.640033",
  });
});

test("SPS parsing covers profiles, cropping, interlace, scaling lists and emulation bytes", () => {
  const baseline = parseSps(makeSps({ widthMbs: 40, heightMapUnits: 30 }));
  assert.equal(`${baseline.width}x${baseline.height}`, "640x480");
  assert.equal(baseline.codec, "avc1.42C01E");

  const fullHd = parseSps(makeSps({ profile: 100, widthMbs: 120, heightMapUnits: 68, crop: { left: 0, right: 0, top: 0, bottom: 4 }, vui: true }));
  assert.equal(`${fullHd.width}x${fullHd.height}`, "1920x1080");

  const interlaced = parseSps(makeSps({ profile: 77, widthMbs: 45, heightMapUnits: 18, frameMbsOnly: 0, crop: { left: 0, right: 0, top: 0, bottom: 0 } }));
  assert.equal(`${interlaced.width}x${interlaced.height}`, "720x576");
  assert.equal(interlaced.frameMbsOnly, false);
  const interlacedCrop = parseSps(makeSps({ profile: 77, widthMbs: 45, heightMapUnits: 18, frameMbsOnly: 0, crop: { left: 0, right: 0, top: 0, bottom: 2 } }));
  assert.equal(interlacedCrop.height, 576 - 2 * 2 * 2);

  const yuv444 = parseSps(makeSps({ profile: 244, chroma: 3, widthMbs: 10, heightMapUnits: 10, crop: { left: 1, right: 2, top: 3, bottom: 4 } }));
  assert.equal(`${yuv444.width}x${yuv444.height}`, `${160 - 3}x${160 - 7}`);
  const separate = parseSps(makeSps({ profile: 244, chroma: 3, separate: 1, widthMbs: 10, heightMapUnits: 10, crop: { left: 1, right: 0, top: 0, bottom: 1 } }));
  assert.equal(`${separate.width}x${separate.height}`, "159x159");
  const mono = parseSps(makeSps({ profile: 100, chroma: 0, widthMbs: 10, heightMapUnits: 10, crop: { left: 1, right: 0, top: 0, bottom: 1 } }));
  assert.equal(`${mono.width}x${mono.height}`, "159x159");
  const yuv422 = parseSps(makeSps({ profile: 122, chroma: 2, widthMbs: 10, heightMapUnits: 10, crop: { left: 1, right: 0, top: 0, bottom: 1 } }));
  assert.equal(`${yuv422.width}x${yuv422.height}`, "158x159");

  const scaled = parseSps(makeSps({ profile: 100, scaling: true, widthMbs: 76, heightMapUnits: 164, crop: { left: 0, right: 5, top: 0, bottom: 1 } }));
  assert.equal(`${scaled.width}x${scaled.height}`, "1206x2622");
  const scaled444 = parseSps(makeSps({ profile: 244, chroma: 3, scaling: true, widthMbs: 8, heightMapUnits: 8 }));
  assert.equal(`${scaled444.width}x${scaled444.height}`, "128x128");

  // A large POC offset puts long zero runs into the RBSP, so emulation prevention bytes appear.
  const epbNal = makeSps({ profile: 66, pocType: 1, pocOffset: -(2 ** 29), pocCycle: [-(2 ** 29), 5], widthMbs: 20, heightMapUnits: 15 });
  assert.ok(hex(epbNal).includes("000003"), "the SPS carries emulation prevention bytes");
  const epb = parseSps(epbNal);
  assert.equal(`${epb.width}x${epb.height}`, "320x240");
  const poc2 = parseSps(makeSps({ pocType: 2, widthMbs: 2, heightMapUnits: 2 }));
  assert.equal(`${poc2.width}x${poc2.height}`, "32x32");
});

test("broken SPS units are refused", () => {
  assert.throws(() => parseSps(REAL_PPS), /Not an SPS/);
  assert.throws(() => parseSps(Buffer.from([0x67, 0x64])), /Not an SPS/);
  assert.throws(() => parseSps(REAL_SPS.subarray(0, 8)), /ends early/);
  assert.throws(() => parseSps(makeSps({ widthMbs: 1, heightMapUnits: 1, crop: { left: 8, right: 8, top: 0, bottom: 0 } })), /no picture/);
});

test("access units are classified and their SPS/PPS extracted", () => {
  const key = analyzeAccessUnit(KEY_FRAME);
  assert.equal(key.key, true);
  assert.deepEqual(key.nalTypes, [7, 8, 5]);
  assert.deepEqual(key.sps, REAL_SPS);
  assert.deepEqual(key.pps, REAL_PPS);
  assert.deepEqual(key.config, Buffer.concat([SC4, REAL_SPS, SC4, REAL_PPS]));

  const p = analyzeAccessUnit(P_FRAME);
  assert.deepEqual(p, { key: false, nalTypes: [1], sps: null, pps: null, config: null });

  const mixed = Buffer.concat([SC3, Buffer.from([0x09, 0xf0]), SC3, Buffer.from([0x06, 0x05, 0x01]), SC4, REAL_SPS, SC3, REAL_PPS, SC3, Buffer.from([0x25, 0xb8]), SC3, Buffer.from([0x25, 0x01])]);
  const m = analyzeAccessUnit(mixed);
  assert.deepEqual(m.nalTypes, [9, 6, 7, 8, 5]);
  assert.equal(m.key, true);
  assert.deepEqual(m.pps, REAL_PPS);

  const spsOnly = analyzeAccessUnit(Buffer.concat([SC4, REAL_SPS, SC4, Buffer.from([0x41, 0x00])]));
  assert.equal(spsOnly.config, null);
  assert.deepEqual(analyzeAccessUnit(Buffer.from([1, 2, 3, 4])).nalTypes, []);
  assert.deepEqual(splitNalUnits(KEY_FRAME).map((n) => n[0] & 0x1f), [7, 8, 5]);
});

// ---------------------------------------------------------------------------
// In-process fake companion gRPC server
// ---------------------------------------------------------------------------

async function fakeServer(t, { frames = [KEY_FRAME, P_FRAME, P_FRAME], intervalMs = 2, video = {}, unary = {} } = {}) {
  const dir = tempDir(t);
  const socketPath = join(dir, "s.sock");
  const state = { calls: [], starts: [], stops: 0, leaks: 0, hidEvents: [], hidCalls: 0, hidEnds: 0, videoCalls: [] };
  const sessions = new Set();
  const server = http2.createServer();
  server.on("session", (session) => {
    sessions.add(session);
    session.on("close", () => sessions.delete(session));
    session.on("error", () => {});
  });
  server.on("stream", (stream, headers) => {
    const method = headers[":path"].split("/").pop();
    state.calls.push({ method, path: headers[":path"], contentType: headers["content-type"], te: headers.te });
    stream.on("error", () => {});
    const parser = new GrpcMessageParser();
    let trailers = { "grpc-status": "0" };
    stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
    stream.on("wantTrailers", () => stream.sendTrailers(trailers));
    const ctx = {
      stream,
      state,
      send: (bytes) => !stream.destroyed && stream.write(grpcFrame(bytes)),
      end: (code = 0, message = "") => {
        trailers = { "grpc-status": String(code) };
        if (message) trailers["grpc-message"] = encodeURIComponent(message);
        if (!stream.destroyed && !stream.writableEnded) stream.end();
      },
    };
    if (method === "video_stream") {
      const call = { timer: null, running: false, sent: 0 };
      state.videoCalls.push(call);
      const stopEncoder = () => {
        clearInterval(call.timer);
        call.running = false;
      };
      stream.on("data", (chunk) => {
        for (const message of parser.push(chunk)) {
          const request = decodeVideoStreamRequest(message);
          if (request.type === "start") {
            state.starts.push(request);
            call.running = true;
            if (video.onStart) {
              video.onStart(ctx, call);
              continue;
            }
            call.timer = setInterval(() => {
              ctx.send(encodeVideoStreamResponse({ data: frames[call.sent % frames.length] }));
              call.sent += 1;
              if (video.limit && call.sent >= video.limit) clearInterval(call.timer);
            }, intervalMs);
          } else if (request.type === "stop") {
            state.stops += 1;
            stopEncoder();
            if (!video.ignoreStop) ctx.end(0);
          }
        }
      });
      stream.on("close", () => {
        // Like idb_companion: a call that goes away without Stop leaves the encoder running.
        if (call.running) state.leaks += 1;
        stopEncoder();
      });
      return;
    }
    if (method === "hid") {
      state.hidCalls += 1;
      stream.on("data", (chunk) => {
        for (const message of parser.push(chunk)) state.hidEvents.push(decodeHidEvent(message));
      });
      stream.on("end", () => {
        state.hidEnds += 1;
        ctx.send(Buffer.alloc(0));
        ctx.end(0);
      });
      return;
    }
    const handler = unary[method];
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        if (!handler) return ctx.end(12, `unknown method ${method}`);
        handler(ctx, message);
      }
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  t.after(async () => {
    for (const session of sessions) session.destroy();
    await new Promise((resolve) => server.close(resolve));
  });
  return { endpoint: { socketPath }, state, sessions };
}

function clientFor(t, endpoint) {
  const client = new IdbCompanionClient(endpoint);
  t.after(() => client.close({ timeoutMs: 200 }));
  return client;
}

test("a video stream yields frames with keyframe marks, SPS/PPS and size, and stop sends Stop", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream({ fps: 60 });
  const frames = [];
  for await (const frame of stream) {
    frames.push(frame);
    if (frames.length === 7) break;
  }
  assert.equal(state.calls[0].path, "/idb.CompanionService/video_stream");
  assert.equal(state.calls[0].contentType, "application/grpc");
  assert.equal(state.calls[0].te, "trailers");
  assert.equal(state.starts.length, 1);
  assert.equal(state.starts[0].fps, 60);
  assert.equal(state.starts[0].format, VIDEO_FORMAT.H264);
  assert.equal(state.starts[0].keyFrameRate, 2);
  assert.deepEqual(frames.map((f) => f.key), [true, false, false, true, false, false, true]);
  assert.deepEqual(frames[0].nalTypes, [7, 8, 5]);
  assert.deepEqual(frames[0].config, Buffer.concat([SC4, REAL_SPS, SC4, REAL_PPS]));
  assert.equal(frames[0].configChanged, true);
  assert.equal(frames[3].configChanged, false, "the same SPS/PPS again is not a change");
  assert.equal(frames[1].config, null);
  for (const frame of frames) {
    assert.equal(frame.width, 1206);
    assert.equal(frame.height, 2622);
    assert.equal(frame.codec, "avc1.640033");
  }
  assert.deepEqual(frames[1].data, P_FRAME);
  for (let i = 1; i < frames.length; i += 1) assert.ok(frames[i].pts >= frames[i - 1].pts);
  assert.deepEqual(stream.config.annexB, frames[0].config);
  assert.equal(stream.stopSent, true);
  assert.equal(stream.abnormal, false);
  assert.equal(state.stops, 1);
  assert.equal(stream.stats.frames >= 7, true);
  assert.equal(stream.stats.keyFrames >= 3, true);
  await waitFor(() => state.leaks === 0 && state.videoCalls[0].running === false, { message: "encoder stopped" });
  assert.deepEqual(await stream.closed, { abnormal: false, stopSent: true, error: null });
  // stop() after the loop is a no-op.
  assert.deepEqual(await stream.stop(), { stopSent: true, abnormal: false, error: null });
  assert.equal(state.stops, 1);
});

test("explicit stop() ends a pending read and sends Stop once", async (t) => {
  const { endpoint, state } = await fakeServer(t, { video: { onStart: () => {} } });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  const pending = stream.next();
  await waitFor(() => state.starts.length === 1, { message: "Start" });
  const [a, b] = await Promise.all([stream.stop(), stream.stop()]);
  assert.deepEqual(a, b);
  assert.equal(a.stopSent, true);
  assert.deepEqual(await pending, { value: undefined, done: true });
  assert.equal(state.stops, 1);
  assert.equal(state.leaks, 0);
});

test("an abort signal stops the stream with Stop and the iterator rejects", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = clientFor(t, endpoint);
  const controller = new AbortController();
  const stream = await client.openVideoStream({}, { signal: controller.signal });
  let count = 0;
  await assert.rejects(async () => {
    for await (const _frame of stream) {
      count += 1;
      if (count === 3) controller.abort();
    }
  }, /aborted/);
  assert.equal(state.stops, 1);
  assert.equal(stream.stopSent, true);
  assert.equal(stream.abnormal, false);
  await waitFor(() => state.videoCalls[0].running === false, { message: "encoder stopped" });
  assert.equal(state.leaks, 0);

  const aborted = AbortSignal.abort();
  await assert.rejects(client.openVideoStream({}, { signal: aborted }), /aborted/);
});

test("an error thrown in the consumer loop still sends Stop", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  await assert.rejects(async () => {
    for await (const _frame of stream) throw new Error("consumer failed");
  }, /consumer failed/);
  assert.equal(state.stops, 1);
  assert.equal(stream.abnormal, false);
  const thrown = await client.openVideoStream();
  await assert.rejects(thrown.throw(new Error("outer")), /outer/);
  assert.equal(state.stops, 2);
});

test("a message that does not decode fails the stream and still sends Stop", async (t) => {
  const { endpoint, state } = await fakeServer(t, {
    video: {
      onStart: (ctx) => {
        ctx.send(encodeVideoStreamResponse({ data: KEY_FRAME }));
        ctx.send(Buffer.from([0x0a, 0x7f, 0x01])); // truncated length-delimited field
      },
    },
  });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  const seen = [];
  await assert.rejects(async () => {
    for await (const frame of stream) seen.push(frame);
  }, /Truncated length-delimited/);
  assert.equal(seen.length, 1, "frames before the failure are delivered");
  assert.equal(state.stops, 1);
  assert.equal(stream.stopSent, true);
  assert.equal(stream.abnormal, false);
  assert.equal(state.leaks, 0);
});

test("a gRPC error status from the companion rejects the iterator and is not abnormal", async (t) => {
  const { endpoint } = await fakeServer(t, {
    video: { onStart: (ctx) => ctx.end(9, "target is not booted: ü%") },
  });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  await assert.rejects(
    async () => {
      for await (const _frame of stream);
    },
    (error) => error instanceof GrpcError && error.code === 9 && error.details === "target is not booted: ü%",
  );
  assert.equal(stream.abnormal, false);
  const closed = await stream.closed;
  assert.equal(closed.error.code, 9);
});

test("a companion that ends the stream by itself finishes the iterator cleanly", async (t) => {
  const { endpoint, state } = await fakeServer(t, {
    video: {
      onStart: (ctx, call) => {
        ctx.send(encodeVideoStreamResponse({ data: KEY_FRAME }));
        ctx.send(encodeVideoStreamResponse({ log: "encoder log line" }));
        ctx.send(encodeVideoStreamResponse({ data: P_FRAME }));
        call.running = false;
        ctx.end(0);
      },
    },
  });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  const frames = [];
  for await (const frame of stream) frames.push(frame);
  assert.deepEqual(frames.map((f) => f.key), [true, false]);
  assert.equal(stream.stats.logs, 1);
  assert.equal(stream.abnormal, false);
  assert.equal(state.stops, 0);
});

test("a dropped call is reported as abnormal so the owner restarts the companion", async (t) => {
  const { endpoint, state } = await fakeServer(t, {
    video: {
      onStart: (ctx) => {
        ctx.send(encodeVideoStreamResponse({ data: KEY_FRAME }));
        setTimeout(() => ctx.stream.close(http2.constants.NGHTTP2_INTERNAL_ERROR), 20);
      },
    },
  });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  const frames = [];
  await assert.rejects(async () => {
    for await (const frame of stream) frames.push(frame);
  });
  assert.equal(frames.length, 1);
  assert.equal(stream.abnormal, true);
  assert.equal(stream.stopSent, false);
  assert.equal(state.stops, 0);
  assert.equal((await stream.closed).abnormal, true);
});

test("a dropped session ends the stream as abnormal and the client reconnects afterwards", async (t) => {
  const { endpoint, state, sessions } = await fakeServer(t, {
    unary: { get_orientation: (ctx) => (ctx.send(Buffer.from(GOLDEN.orientation, "hex")), ctx.end(0)) },
  });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  await stream.next();
  for (const session of sessions) session.destroy();
  await assert.rejects(async () => {
    for await (const _frame of stream);
  });
  assert.equal(stream.abnormal, true);
  await waitFor(() => state.leaks === 1, { message: "leak recorded" });
  assert.equal(await client.getOrientation(), "LANDSCAPE_LEFT");
});

test("an unreachable endpoint fails calls instead of hanging; a closed client refuses new calls", async (t) => {
  const dir = tempDir(t);
  const client = clientFor(t, { socketPath: join(dir, "missing.sock") });
  await assert.rejects(client.getOrientation({ timeoutMs: 2000 }), (error) => error.code !== 4);
  const stream = await client.openVideoStream();
  await assert.rejects(stream.next());
  assert.equal(stream.done, true);
  assert.equal(stream.stopSent, false);
  await client.close();
  await assert.rejects(client.getOrientation(), /closed/);
  await assert.rejects(client.openVideoStream(), /closed/);
});

test("restart() sends Stop, then a new Start, and the same iterator continues", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream({ keyFrameRate: 1 });
  const first = await stream.next();
  assert.equal(first.value.key, true);
  await stream.restart();
  assert.equal(state.stops, 1);
  await waitFor(() => state.starts.length === 2, { message: "second Start" });
  assert.equal(state.starts[1].keyFrameRate, 1);
  // Drain anything the first call delivered before Stop, then expect the new keyframe.
  let sawKey = false;
  for (let i = 0; i < 20 && !sawKey; i += 1) {
    const { value } = await stream.next();
    sawKey = value.key && value.nalTypes[0] === 7;
  }
  assert.ok(sawKey);
  assert.equal(stream.stats.calls, 2);
  assert.equal(stream.abnormal, false);
  await stream.stop();
  assert.equal(state.stops, 2);
  await waitFor(() => state.videoCalls.every((c) => !c.running), { message: "both encoders stopped" });
  assert.equal(state.leaks, 0);
  await assert.rejects(stream.restart(), /closed/);
});

/** A video call whose Stop write fails, as on a call the transport can no longer carry. */
class StopFailingCall extends EventEmitter {
  writable = true;
  destroyed = false;
  closed = false;
  writes = [];

  write(chunk, callback) {
    const request = decodeVideoStreamRequest(new GrpcMessageParser().push(chunk)[0]);
    this.writes.push(request.type);
    if (request.type === "stop") setImmediate(() => callback?.(new Error("write EPIPE")));
    else callback?.();
    return true;
  }

  end() {
    this.writable = false;
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.destroyed = true;
    setImmediate(() => this.emit("close"));
  }

  pause() {}

  resume() {}
}

test("a restart whose Stop cannot be written ends the stream as abnormal and opens no second call", async () => {
  const calls = [];
  const client = { openCall: () => calls.at(calls.push(new StopFailingCall()) - 1) };
  const stream = new IdbVideoStream(client);
  await stream.start();
  await assert.rejects(stream.restart({ timeoutMs: 200 }), /Stop was not written/);
  assert.equal(calls.length, 1, "a second Start went out on a companion whose encoder may still run");
  assert.deepEqual(calls[0].writes, ["start", "stop"]);
  assert.equal(calls[0].closed, true);
  assert.equal(stream.done, true);
  assert.equal(stream.abnormal, true);
  assert.equal(stream.stopSent, false);
  const closed = await stream.closed;
  assert.equal(closed.abnormal, true);
  assert.match(closed.error.message, /companion must be restarted/);
  // The reader sees the end: the owner replaces the companion before the next stream.
  await assert.rejects(stream.next(), /Stop was not written/);
  assert.deepEqual(await stream.next(), { value: undefined, done: true });
});

test("a restart whose Stop the companion does not answer ends the stream as abnormal, with no second Start", async (t) => {
  const { endpoint, state } = await fakeServer(t, { video: { ignoreStop: true } });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  await stream.next();
  const started = Date.now();
  await assert.rejects(stream.restart({ timeoutMs: 150 }), /Stop was not answered/);
  assert.ok(Date.now() - started < 1000);
  assert.equal(state.stops, 1);
  assert.equal(state.starts.length, 1);
  assert.equal(stream.stats.calls, 1);
  assert.equal((await stream.closed).abnormal, true);
  await sleep(50);
  assert.equal(state.starts.length, 1);
});

test("stop() waiting on an unresponsive companion is bounded", async (t) => {
  const { endpoint, state } = await fakeServer(t, { video: { ignoreStop: true } });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  await stream.next();
  const started = Date.now();
  const result = await stream.stop({ timeoutMs: 150 });
  assert.ok(Date.now() - started < 1000);
  assert.equal(result.stopSent, true);
  assert.equal(state.stops, 1);
});

test("a slow consumer gets every frame in order (flow control, no drops)", async (t) => {
  const numbered = Array.from({ length: 40 }, (_, i) => Buffer.concat([SC4, Buffer.from([0x41, i + 1])]));
  const { endpoint } = await fakeServer(t, { frames: numbered, intervalMs: 1, video: { limit: 40 } });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream();
  const seen = [];
  for await (const frame of stream) {
    seen.push(frame.data.at(-1));
    if (seen.length % 5 === 0) await sleep(15);
    if (seen.length === 40) break;
  }
  assert.ok(FRAME_QUEUE_HIGH_WATER < 40);
  assert.deepEqual(seen, Array.from({ length: 40 }, (_, i) => i + 1));
});

test("MJPEG frames are passed through as keyframes", async (t) => {
  const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
  const { endpoint, state } = await fakeServer(t, { frames: [jpeg] });
  const client = clientFor(t, endpoint);
  const stream = await client.openVideoStream({ format: "mjpeg" });
  const { value } = await stream.next();
  await stream.stop();
  assert.equal(state.starts[0].format, VIDEO_FORMAT.MJPEG);
  assert.deepEqual(value.data, jpeg);
  assert.equal(value.key, true);
  assert.equal(value.width, null);
});

test("client.close() stops every open stream with Stop before closing", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = new IdbCompanionClient(endpoint);
  const a = await client.openVideoStream();
  const b = await client.openVideoStream();
  await a.next();
  await b.next();
  await client.hid().touchDown(1, 2);
  await client.close();
  assert.equal(state.stops, 2);
  assert.equal(a.stopSent && b.stopSent, true);
  await waitFor(() => state.hidEnds === 1, { message: "hid ended" });
  await waitFor(() => state.videoCalls.every((c) => !c.running), { message: "encoders stopped" });
  assert.equal(state.leaks, 0);
  await assert.rejects(client.openVideoStream(), /closed/);
  assert.throws(() => client.hid(), /closed/);
  await client.close();
});

test("a stream opened while close() is stopping the others is refused, so none leaks", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = new IdbCompanionClient(endpoint);
  const a = await client.openVideoStream();
  await a.next();
  const closing = client.close({ timeoutMs: 300 });
  assert.equal(client.closed, true, "closed as soon as close() begins");
  await assert.rejects(client.openVideoStream(), /closed/);
  assert.throws(() => client.hid(), /closed/);
  await assert.rejects(client.getOrientation(), /closed/);
  await client.close();
  assert.equal(a.stopSent, true, "a second close() waits for the first one");
  await closing;
  await waitFor(() => state.videoCalls.every((c) => !c.running), { message: "encoders stopped" });
  assert.equal(state.starts.length, 1);
  assert.equal(state.stops, 1);
  assert.equal(state.leaks, 0);
});

test("HID events go out in order on one open call, and a new call opens after it ends", async (t) => {
  const { endpoint, state } = await fakeServer(t);
  const client = clientFor(t, endpoint);
  const hid = client.hid();
  assert.equal(client.hid(), hid);
  await hid.touchDown(200, 650);
  await hid.touchMove(200, 600.5);
  await hid.touchUp(200, 600.5);
  await hid.tap(10, 20);
  await hid.pressButton("HOME");
  await hid.buttonDown("LOCK");
  await hid.buttonUp("LOCK");
  await hid.keyDown(4);
  await hid.keyUp(4);
  await hid.pinch({ center: { x: 201, y: 437 }, scale: 1.5, duration: 0.4, radius: 40 });
  await waitFor(() => state.hidEvents.length === 12, { message: "12 HID events" });
  assert.equal(state.hidCalls, 1);
  assert.deepEqual(state.hidEvents, [
    { touch: { x: 200, y: 650 }, direction: "down" },
    { touch: { x: 200, y: 600.5 }, direction: "down" },
    { touch: { x: 200, y: 600.5 }, direction: "up" },
    { touch: { x: 10, y: 20 }, direction: "down" },
    { touch: { x: 10, y: 20 }, direction: "up" },
    { button: "HOME", direction: "down" },
    { button: "HOME", direction: "up" },
    { button: "LOCK", direction: "down" },
    { button: "LOCK", direction: "up" },
    { key: 4, direction: "down" },
    { key: 4, direction: "up" },
    { pinch: { center: { x: 201, y: 437 }, scale: 1.5, duration: 0.4, radius: 40 } },
  ]);
  assert.equal(hid.sent, 12);
  await assert.rejects(hid.touchDown(-5, 0), RangeError);
  await hid.close();
  await waitFor(() => state.hidEnds === 1, { message: "hid end" });
  await assert.rejects(hid.touchDown(1, 1), /closed/);
});

test("the HID stream reopens after the companion drops it", async (t) => {
  const { endpoint, state, sessions } = await fakeServer(t);
  const client = clientFor(t, endpoint);
  const hid = client.hid();
  await hid.touchDown(1, 1);
  await waitFor(() => state.hidEvents.length === 1, { message: "first event" });
  for (const session of sessions) session.destroy();
  await waitFor(() => sessions.size === 0, { message: "session gone" });
  await sleep(20);
  await hid.touchUp(1, 1);
  await waitFor(() => state.hidEvents.length === 2, { message: "event on the new call" });
  assert.equal(state.hidCalls, 2);
});

test("describe and get_orientation are unary calls; errors and deadlines map to GrpcError", async (t) => {
  const { endpoint, state } = await fakeServer(t, {
    unary: {
      describe: (ctx, message) => {
        ctx.state.describeRequest = message;
        ctx.send(Buffer.from(GOLDEN.describe, "hex"));
        ctx.end(0);
      },
      get_orientation: (ctx) => ctx.end(14, "companion busy"),
      list_apps: () => {}, // never answers
      screenshot: (ctx) => ctx.end(0), // OK status without a message
    },
  });
  const client = clientFor(t, endpoint);
  const description = await client.describe();
  assert.equal(description.screen.widthPoints, 402);
  assert.equal(description.displays.length, 1);
  assert.equal(state.describeRequest.length, 0);
  await assert.rejects(client.getOrientation(), (error) => error instanceof GrpcError && error.code === 14 && /companion busy/.test(error.message));
  await assert.rejects(client.unary("list_apps", Buffer.alloc(0), { timeoutMs: 100 }), (error) => error.code === 4);
  await assert.rejects(client.unary("screenshot", Buffer.alloc(0)), (error) => error.code === 13);
  await assert.rejects(client.unary("nothing_here", Buffer.alloc(0)), (error) => error.code === 12);
  const controller = new AbortController();
  const pending = client.unary("list_apps", Buffer.alloc(0), { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, /aborted/);
  assert.throws(() => new IdbCompanionClient({}), TypeError);
});

test("the client speaks to a TCP endpoint too", async (t) => {
  const server = http2.createServer();
  server.on("stream", (stream) => {
    stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
    stream.on("wantTrailers", () => stream.sendTrailers({ "grpc-status": "0" }));
    stream.on("data", () => {
      stream.write(grpcFrame(Buffer.from(GOLDEN.orientation, "hex")));
      stream.end();
    });
  });
  const sessions = new Set();
  server.on("session", (session) => {
    sessions.add(session);
    session.on("close", () => sessions.delete(session));
    session.on("error", () => {});
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  // This hook runs before the client's: on Node 22, server.close() waits for open
  // sessions instead of closing idle ones, so end them here like fakeServer() does.
  t.after(async () => {
    for (const session of sessions) session.destroy();
    await new Promise((resolve) => server.close(resolve));
  });
  const client = clientFor(t, { port: server.address().port });
  assert.equal(await client.getOrientation(), "LANDSCAPE_LEFT");
  const port = await freeLoopbackPort();
  assert.ok(Number.isInteger(port) && port > 0);
});

// ---------------------------------------------------------------------------
// Companion process with a fake idb_companion executable
// ---------------------------------------------------------------------------

const FAKE_COMPANION = String.raw`
import { spawn } from "node:child_process";
import { appendFileSync } from "node:fs";
import * as http2 from "node:http2";
import { GrpcMessageParser, decodeVideoStreamRequest, encodeVideoStreamResponse, grpcFrame } from "${MODULE_URL}";

const args = process.argv.slice(2);
const arg = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const events = process.env.FAKE_EVENTS;
const record = (event) => events && appendFileSync(events, JSON.stringify({ pid: process.pid, ...event }) + "\n");
const mode = process.env.FAKE_MODE ?? "ok";
record({ type: "spawn", args });
process.stderr.write("fake companion log line\n");
if (mode === "crash") { process.stderr.write("cannot find target\n"); process.exit(3); }
const helper = spawn("sleep", ["60"], { stdio: "ignore" });
record({ type: "helper", helperPid: helper.pid });
process.on("SIGTERM", () => {
  record({ type: "term" });
  if (process.env.FAKE_IGNORE_TERM) return;
  helper.kill();
  process.exit(0);
});
if (mode === "silent") setInterval(() => {}, 1000);
else {
  const server = http2.createServer();
  server.on("stream", (stream, headers) => {
    const method = headers[":path"].split("/").pop();
    const parser = new GrpcMessageParser();
    stream.on("error", () => {});
    stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
    stream.on("wantTrailers", () => stream.sendTrailers({ "grpc-status": "0" }));
    let timer = null;
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        if (method !== "video_stream") { stream.write(grpcFrame(Buffer.from("0803", "hex"))); stream.end(); continue; }
        const request = decodeVideoStreamRequest(message);
        record({ type: request.type });
        if (request.type === "start") timer = setInterval(() => stream.write(grpcFrame(encodeVideoStreamResponse({ data: Buffer.from([0, 0, 0, 1, 0x41, 1]) }))), 5);
        else { clearInterval(timer); stream.end(); }
      }
    });
    stream.on("close", () => clearInterval(timer));
  });
  const socketPath = arg("--grpc-domain-sock");
  const ready = () => process.stdout.write(JSON.stringify(socketPath ? { grpc_path: socketPath } : { grpc_swift_port: Number(arg("--grpc-port")), grpc_port: Number(arg("--grpc-port")) }) + "\n");
  if (socketPath) server.listen(socketPath, ready);
  else server.listen(Number(arg("--grpc-port")), "127.0.0.1", ready);
}
`;

function fakeCompanion(t, env = {}) {
  const dir = tempDir(t, "idbf-");
  const binary = join(dir, "idb_companion");
  writeFileSync(binary, `#!${process.execPath}\n${FAKE_COMPANION}`);
  chmodSync(binary, 0o755);
  const events = join(dir, "events.jsonl");
  writeFileSync(events, "");
  const read = () =>
    readFileSync(events, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  const companionEnv = { ...process.env, FAKE_EVENTS: events, ...env };
  return { dir, binary, env: companionEnv, events: read };
}

function makeCompanion(t, fake, options = {}) {
  const companion = new IdbCompanion({ udid: UDID, binary: fake.binary, env: fake.env, socketDir: fake.dir, ...options });
  t.after(() => companion.stop());
  return companion;
}

test("the companion starts on a private socket, serves gRPC and stops with SIGTERM, helpers included", async (t) => {
  const fake = fakeCompanion(t);
  const logPath = join(fake.dir, "companion.log");
  const companion = makeCompanion(t, fake, { logPath });
  const lines = [];
  companion.on("log", (line) => lines.push(line));
  const client = await companion.start();
  assert.equal(await companion.start(), client, "start() while running returns the same client");
  const pid = companion.pid;
  assert.ok(companion.running);
  const { socketPath } = companion.endpoint;
  assert.ok(socketPath.startsWith(join(fake.dir, "autonom-idb-")));
  assert.equal(statSync(join(socketPath, "..")).mode & 0o777, 0o700);
  const spawnEvent = fake.events().find((e) => e.type === "spawn");
  assert.deepEqual(spawnEvent.args, ["--udid", UDID, "--grpc-domain-sock", socketPath, "--only", "simulator"]);
  assert.equal(await client.getOrientation(), "LANDSCAPE_LEFT");

  const stream = await client.openVideoStream();
  const { value } = await stream.next();
  assert.deepEqual(value.nalTypes, [1]);
  const helperPid = fake.events().find((e) => e.type === "helper").helperPid;
  assert.ok(alive(helperPid));

  const result = await companion.stop();
  assert.deepEqual(result, { code: 0, signal: null, exited: true });
  const types = fake.events().map((e) => e.type);
  assert.ok(types.indexOf("stop") > types.indexOf("start"), "Stop reached the companion");
  assert.ok(types.indexOf("term") > types.indexOf("stop"), "Stop went out before SIGTERM");
  assert.equal(stream.stopSent, true);
  assert.equal(companion.running, false);
  assert.equal(companion.client, null);
  assert.equal(alive(pid), false);
  await waitFor(() => !alive(helperPid), { message: "helper killed" });
  assert.equal(existsSync(join(socketPath, "..")), false, "the private socket directory is removed");
  assert.ok(lines.includes("fake companion log line"));
  assert.ok(companion.logTail().some((line) => line.includes("grpc_path")));
  await waitFor(() => existsSync(logPath) && readFileSync(logPath, "utf8").includes("fake companion log line"), { message: "log file" });
  assert.deepEqual(await companion.stop(), result);
});

test("a companion that ignores SIGTERM is killed after the stop timeout", async (t) => {
  const fake = fakeCompanion(t, { FAKE_IGNORE_TERM: "1" });
  const companion = makeCompanion(t, fake, { stopTimeoutMs: 200 });
  await companion.start();
  const pid = companion.pid;
  const helperPid = fake.events().find((e) => e.type === "helper").helperPid;
  const started = Date.now();
  const result = await companion.stop();
  assert.ok(Date.now() - started >= 180);
  assert.equal(result.signal, "SIGKILL");
  assert.ok(fake.events().some((e) => e.type === "term"));
  assert.equal(alive(pid), false);
  await waitFor(() => !alive(helperPid), { message: "helper killed with the group" });
});

test("restart() replaces the companion process", async (t) => {
  const fake = fakeCompanion(t);
  const companion = makeCompanion(t, fake);
  const first = await companion.start();
  const firstPid = companion.pid;
  const firstSocket = companion.endpoint.socketPath;
  const second = await companion.restart();
  assert.notEqual(second, first);
  assert.notEqual(companion.pid, firstPid);
  assert.notEqual(companion.endpoint.socketPath, firstSocket);
  assert.equal(alive(firstPid), false);
  assert.equal(first.closed, true);
  assert.equal(await second.getOrientation(), "LANDSCAPE_LEFT");
});

test("start fails cleanly when the companion exits early, never gets ready, or is missing", async (t) => {
  const crash = fakeCompanion(t, { FAKE_MODE: "crash" });
  const crashing = makeCompanion(t, crash);
  await assert.rejects(crashing.start(), /exited before it was ready \(code 3.*cannot find target/);
  assert.equal(crashing.running, false);

  const silent = fakeCompanion(t, { FAKE_MODE: "silent" });
  const hanging = makeCompanion(t, silent, { readyTimeoutMs: 300, stopTimeoutMs: 300 });
  await assert.rejects(hanging.start(), /not ready within 300 ms/);
  const pid = silent.events().find((e) => e.type === "spawn").pid;
  assert.equal(alive(pid), false);
  assert.ok(silent.events().some((e) => e.type === "term"));

  const missing = new IdbCompanion({ udid: UDID, binary: join(crash.dir, "no-such-companion") });
  await assert.rejects(missing.start(), (error) => error.message === IDB_INSTALL_HINT);
  assert.equal(missing.running, false);
});

test("an unexpected companion exit is reported and the next start spawns a new one", async (t) => {
  const fake = fakeCompanion(t);
  const companion = makeCompanion(t, fake);
  await companion.start();
  const exits = [];
  companion.on("exit", (info) => exits.push(info));
  const pid = companion.pid;
  process.kill(pid, "SIGKILL");
  const helperPid = fake.events().find((e) => e.type === "helper").helperPid;
  await waitFor(() => exits.length === 1, { message: "exit event" });
  assert.deepEqual(exits[0], { code: null, signal: "SIGKILL", expected: false });
  await waitFor(() => !alive(helperPid), { message: "orphaned helper killed with the group" });
  assert.equal(companion.running, false);
  const client = await companion.start();
  assert.notEqual(companion.pid, pid);
  assert.equal(await client.getOrientation(), "LANDSCAPE_LEFT");
});

const groupKills = (killMock, pid) =>
  killMock.mock.calls.filter((call) => call.arguments[0] === -pid).map((call) => call.arguments[1]);

test("the process group is signalled only until it is reaped; later stop() calls leave the id alone", async (t) => {
  const kill = t.mock.method(process, "kill");
  const fake = fakeCompanion(t);
  const companion = makeCompanion(t, fake);
  await companion.start();
  const pid = companion.pid;
  await companion.stop();
  assert.deepEqual(groupKills(kill, pid), ["SIGTERM", "SIGKILL"]);
  await sleep(50);
  await companion.stop();
  companion.killSync();
  await companion.stop();
  assert.deepEqual(groupKills(kill, pid), ["SIGTERM", "SIGKILL"], "a reaped group id is never signalled again");
});

test("after an unexpected exit the group is killed once; stop() and restart() do not signal it again", async (t) => {
  const kill = t.mock.method(process, "kill");
  const fake = fakeCompanion(t);
  const companion = makeCompanion(t, fake);
  await companion.start();
  const exits = [];
  companion.on("exit", (info) => exits.push(info));
  const pid = companion.pid;
  process.kill(pid, "SIGKILL");
  await waitFor(() => exits.length === 1, { message: "exit event" });
  assert.deepEqual(groupKills(kill, pid), ["SIGKILL"], "helpers are cleared once");
  await companion.stop();
  await companion.restart();
  const newPid = companion.pid;
  assert.notEqual(newPid, pid);
  await companion.stop();
  await companion.stop();
  assert.deepEqual(groupKills(kill, pid), ["SIGKILL"]);
  assert.deepEqual(groupKills(kill, newPid), ["SIGTERM", "SIGKILL"]);

  const crash = fakeCompanion(t, { FAKE_MODE: "crash" });
  const crashing = makeCompanion(t, crash);
  await assert.rejects(crashing.start(), /exited before it was ready/);
  await crashing.stop();
  const crashPid = crash.events().find((e) => e.type === "spawn").pid;
  assert.deepEqual(groupKills(kill, crashPid), ["SIGKILL"], "an early exit is cleared once too");
});

test("killSync() then restart() waits for the old exit before the new companion starts", async (t) => {
  const fake = fakeCompanion(t);
  const companion = makeCompanion(t, fake);
  await companion.start();
  const exits = [];
  companion.on("exit", (info) => exits.push({ ...info, pid: companion.pid }));
  const oldPid = companion.pid;
  const oldHelper = fake.events().find((e) => e.type === "helper").helperPid;
  companion.killSync();
  const client = await companion.restart();
  const newPid = companion.pid;
  assert.notEqual(newPid, oldPid);
  assert.deepEqual(exits, [{ code: null, signal: "SIGKILL", expected: true, pid: oldPid }], "the old exit landed first");
  assert.equal(alive(oldPid), false);
  await waitFor(() => !alive(oldHelper), { message: "old helper killed with its group" });
  await sleep(300);
  assert.equal(exits.length, 1, "no late exit event for the new companion");
  assert.ok(companion.running);
  assert.equal(await client.getOrientation(), "LANDSCAPE_LEFT");
  reapAllCompanionsSync();
  await waitFor(() => !alive(newPid), { message: "the new companion is still reaped on host exit" });
  await waitFor(() => exits.length === 2, { message: "exit of the new companion" });
  assert.deepEqual(exits[1], { code: null, signal: "SIGKILL", expected: true, pid: newPid });
});

test("a late exit of a replaced child leaves the new companion's state and events alone", async (t) => {
  // Children that outlive SIGKILL for a while; process.kill is mocked, so no real group is signalled.
  const kill = t.mock.method(process, "kill", () => true);
  const children = [];
  const spawnFake = (binary, args) => {
    const child = new EventEmitter();
    child.pid = 4_000_000 + children.length;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    children.push(child);
    const socketPath = args[args.indexOf("--grpc-domain-sock") + 1];
    setImmediate(() => child.stdout.write(`${JSON.stringify({ grpc_path: socketPath })}\n`));
    return child;
  };
  const companion = new IdbCompanion({ udid: UDID, socketDir: tempDir(t), spawn: spawnFake, stopTimeoutMs: 50 });
  await companion.start();
  const exits = [];
  companion.on("exit", (info) => exits.push(info));
  companion.killSync();
  await companion.restart(); // the old child never exits here, so this waits KILL_WAIT_MS and moves on
  assert.equal(children.length, 2);
  const [old, current] = children;
  old.emit("exit", null, "SIGKILL");
  assert.deepEqual(exits, [], "the stale exit is not reported as the current companion's");
  assert.ok(companion.running);
  assert.equal(companion.pid, current.pid);
  const before = kill.mock.callCount();
  reapAllCompanionsSync();
  const calls = kill.mock.calls.slice(before).map((call) => call.arguments);
  assert.deepEqual(calls, [[-current.pid, "SIGKILL"]], "the new companion is still tracked for exit-time reaping");
  current.emit("exit", null, "SIGKILL");
  assert.deepEqual(exits, [{ code: null, signal: "SIGKILL", expected: true }]);
  assert.equal(companion.running, false);
  await companion.stop();
  const oldSignals = kill.mock.calls.filter((call) => call.arguments[0] === -old.pid).map((call) => call.arguments[1]);
  assert.deepEqual(oldSignals, ["SIGKILL"], "the old group got one SIGKILL only");
});

test("the TCP transport passes a free port and reads the port readiness line", async (t) => {
  const fake = fakeCompanion(t);
  const companion = makeCompanion(t, fake, { transport: "tcp", only: null });
  const client = await companion.start();
  const { port, host } = companion.endpoint;
  assert.equal(host, "127.0.0.1");
  const args = fake.events().find((e) => e.type === "spawn").args;
  assert.deepEqual(args, ["--udid", UDID, "--grpc-port", String(port)]);
  assert.equal(await client.getOrientation(), "LANDSCAPE_LEFT");
});

test("bad companion options are refused before anything is spawned", (t) => {
  assert.throws(() => new IdbCompanion({ udid: "--help" }), TypeError);
  assert.throws(() => new IdbCompanion({ udid: "" }), TypeError);
  assert.throws(() => new IdbCompanion({ udid: UDID, transport: "udp" }), RangeError);
  const deep = tempDir(t);
  const longDir = join(deep, "x".repeat(120));
  const companion = new IdbCompanion({ udid: UDID, socketDir: longDir });
  return assert.rejects(companion.start(), /ENOENT|longer than/);
});

test("a host process that exits without stop() still takes the companion and its helpers down", async (t) => {
  const fake = fakeCompanion(t);
  const script = join(fake.dir, "host.mjs");
  writeFileSync(
    script,
    `import { IdbCompanion } from ${JSON.stringify(MODULE_URL)};
const companion = new IdbCompanion({ udid: ${JSON.stringify(UDID)}, binary: ${JSON.stringify(fake.binary)}, socketDir: ${JSON.stringify(fake.dir)} });
await companion.start();
console.log(JSON.stringify({ pid: companion.pid, socketPath: companion.endpoint.socketPath }));
process.exit(0);
`,
  );
  const child = spawn(process.execPath, [script], { env: fake.env, stdio: ["ignore", "pipe", "inherit"] });
  let out = "";
  child.stdout.on("data", (chunk) => {
    out += chunk;
  });
  const code = await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(code, 0);
  const { pid, socketPath } = JSON.parse(out.trim());
  const helperPid = fake.events().find((e) => e.type === "helper").helperPid;
  await waitFor(() => !alive(pid), { message: "companion reaped on host exit" });
  await waitFor(() => !alive(helperPid), { message: "helper reaped on host exit" });
  assert.equal(existsSync(join(socketPath, "..")), false);
});


// ---------------------------------------------------------------------------
// The Canvas's idb_companion binary
// ---------------------------------------------------------------------------

function executable(path) {
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, "#!/bin/sh\nexit 0\n");
  chmodSync(path, 0o755);
  return path;
}

test("the binary is --idb-companion, else AUTONOM_IDB_COMPANION_BIN, else PATH; AUTONOM_IDB_COMPANION is never read", async (t) => {
  const dir = tempDir(t);
  const first = executable(join(dir, "first", "idb_companion"));
  const second = executable(join(dir, "second", "idb_companion"));
  const plain = join(dir, "plain", "idb_companion");
  mkdirSync(join(dir, "plain"));
  writeFileSync(plain, "not executable\n");
  const folder = join(dir, "folder");
  mkdirSync(join(folder, "idb_companion"), { recursive: true });
  const empty = join(dir, "empty");
  mkdirSync(empty);
  const PATH = ["", folder, join(dir, "plain"), join(dir, "first"), join(dir, "second")].join(delimiter);
  assert.equal(IDB_COMPANION_BIN_ENV, "AUTONOM_IDB_COMPANION_BIN");

  assert.deepEqual(await resolveIdbCompanionBinary({ flag: second, env: { [IDB_COMPANION_BIN_ENV]: first, PATH } }),
    { available: true, path: second, source: "--idb-companion", reason: null });
  assert.deepEqual(await resolveIdbCompanionBinary({ env: { [IDB_COMPANION_BIN_ENV]: second, PATH } }),
    { available: true, path: second, source: IDB_COMPANION_BIN_ENV, reason: null });
  // Empty PATH entries, a folder and a plain file named idb_companion are passed over.
  assert.deepEqual(await resolveIdbCompanionBinary({ env: { PATH } }),
    { available: true, path: first, source: "PATH", reason: null });
  assert.deepEqual(await resolveIdbCompanionBinary({ env: { [IDB_COMPANION_BIN_ENV]: "", PATH } }),
    { available: true, path: first, source: "PATH", reason: null });

  // A set flag or variable that names no executable file is reported, never skipped for PATH.
  for (const bad of [plain, folder, join(dir, "missing")]) {
    assert.deepEqual(await resolveIdbCompanionBinary({ flag: bad, env: { PATH } }),
      { available: false, path: bad, source: "--idb-companion", reason: `--idb-companion is not an executable file: ${bad}` });
    assert.deepEqual(await resolveIdbCompanionBinary({ env: { [IDB_COMPANION_BIN_ENV]: bad, PATH } }), {
      available: false, path: bad, source: IDB_COMPANION_BIN_ENV,
      reason: `AUTONOM_IDB_COMPANION_BIN is not an executable file: ${bad}`,
    });
  }

  // AUTONOM_IDB_COMPANION is the remote companion of idb calls: a host:port or even a
  // file path there is not the Canvas's binary, and the variable is left as it was.
  for (const remote of ["mac-farm-01:10882", first]) {
    const env = { AUTONOM_IDB_COMPANION: remote, PATH: empty };
    assert.deepEqual(await resolveIdbCompanionBinary({ env }), {
      available: false, path: null, source: null,
      reason: "idb_companion was not found (--idb-companion, AUTONOM_IDB_COMPANION_BIN or PATH)",
    });
    assert.deepEqual(env, { AUTONOM_IDB_COMPANION: remote, PATH: empty });
  }
  assert.equal((await resolveIdbCompanionBinary({ env: { AUTONOM_IDB_COMPANION: "mac-farm-01:10882", PATH } })).path, first);
});

test("the idb refusal is the CLI's JSON error object: tool_missing, canvas.idb, idb_companion and the hint", () => {
  const error = idbUnavailableError("idb_companion was not found (--idb-companion, AUTONOM_IDB_COMPANION_BIN or PATH)");
  assert.ok(error instanceof StructuredError);
  assert.equal(error.code, ERROR_CODE.TOOL_MISSING);
  assert.equal(ERROR_CODE.TOOL_MISSING, "tool_missing");
  assert.deepEqual(JSON.parse(JSON.stringify(error)), {
    ok: false,
    error_code: "tool_missing",
    error: "--transport idb is unavailable: idb_companion was not found (--idb-companion, AUTONOM_IDB_COMPANION_BIN or PATH)",
    hint: IDB_COMPANION_HINT,
    tool: "idb_companion",
    capability: IDB_CAPABILITY,
  });
  assert.match(IDB_COMPANION_HINT, /brew install facebook\/fb\/idb-companion/);
  assert.match(IDB_COMPANION_HINT, /AUTONOM_IDB_COMPANION_BIN/);
  assert.equal(IDB_CAPABILITY, "canvas.idb");
});
