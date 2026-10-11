// Generated H.264 test pattern of the ChatGPT probe (contract 3.5): bit-level checks of
// SPS, PPS and the slice header, emulation prevention, determinism and an optional real
// decoder check with ffprobe.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  addEmulationPrevention,
  codecString,
  configAnnexB,
  encodeIdrFrame,
  encodePps,
  encodeSps,
  makeFrame,
  renderPattern,
} from "../integrations/chatgpt/autonom-chatgpt-probe/media/h264-pcm.mjs";

const WIDTH = 96;
const HEIGHT = 208;
const MBS = (WIDTH / 16) * (HEIGHT / 16);

/** Split an Annex B stream into NAL units (start codes removed). */
function splitAnnexB(buffer) {
  const starts = [];
  for (let i = 0; i + 2 < buffer.length; i += 1) {
    if (buffer[i] === 0 && buffer[i + 1] === 0 && buffer[i + 2] === 1) {
      starts.push({ at: i, payload: i + 3 });
      i += 2;
    }
  }
  return starts.map((start, index) => {
    let end = index + 1 < starts.length ? starts[index + 1].at : buffer.length;
    while (end > start.payload && buffer[end - 1] === 0) end -= 1;
    return buffer.subarray(start.payload, end);
  });
}

function removeEmulationPrevention(ebsp) {
  const out = [];
  let zeros = 0;
  for (const byte of ebsp) {
    if (zeros >= 2 && byte === 3) {
      zeros = 0;
      continue;
    }
    out.push(byte);
    zeros = byte === 0 ? zeros + 1 : 0;
  }
  return Uint8Array.from(out);
}

class BitReader {
  constructor(bytes) {
    this.bytes = bytes;
    this.pos = 0;
  }
  bit() {
    const byte = this.bytes[this.pos >> 3];
    assert.ok(byte !== undefined, "read past the end of the RBSP");
    const value = (byte >> (7 - (this.pos & 7))) & 1;
    this.pos += 1;
    return value;
  }
  u(count) {
    let value = 0;
    for (let i = 0; i < count; i += 1) value = value * 2 + this.bit();
    return value;
  }
  ue() {
    let zeros = 0;
    while (this.bit() === 0) zeros += 1;
    return (1 << zeros) - 1 + this.u(zeros);
  }
  se() {
    const code = this.ue();
    if (code % 2) return (code + 1) / 2;
    return code === 0 ? 0 : -(code / 2);
  }
  get aligned() {
    return (this.pos & 7) === 0;
  }
  trailing() {
    assert.equal(this.bit(), 1, "rbsp_stop_one_bit");
    while (!this.aligned) assert.equal(this.bit(), 0, "rbsp_alignment_zero_bit");
    assert.equal(this.pos >> 3, this.bytes.length, "nothing after rbsp_trailing_bits");
  }
}

function rbspOf(nal) {
  return removeEmulationPrevention(nal.subarray(1));
}

test("every access unit is SPS, PPS, IDR in that order with start codes", () => {
  for (const frame of [0, 1, 2, 77]) {
    const { annexB, key } = makeFrame({ width: WIDTH, height: HEIGHT, frame, timeMs: 1_760_000_000_000 + frame });
    assert.equal(key, true);
    assert.deepEqual([...annexB.subarray(0, 4)], [0, 0, 0, 1]);
    const nals = splitAnnexB(annexB);
    assert.deepEqual(
      nals.map((nal) => nal[0] & 0x1f),
      [7, 8, 5],
    );
    for (const nal of nals) {
      assert.equal(nal[0] & 0x80, 0, "forbidden_zero_bit");
      assert.equal((nal[0] >> 5) & 3, 3, "nal_ref_idc 3");
    }
  }
  const config = splitAnnexB(configAnnexB({ width: WIDTH, height: HEIGHT }));
  assert.deepEqual(
    config.map((nal) => nal[0] & 0x1f),
    [7, 8],
  );
});

test("SPS fields match the contract bit for bit", () => {
  const sps = encodeSps({ width: WIDTH, height: HEIGHT });
  const r = new BitReader(rbspOf(sps));
  assert.equal(r.u(8), 66, "profile_idc");
  assert.equal(r.u(1), 1, "constraint_set0_flag");
  assert.equal(r.u(1), 1, "constraint_set1_flag");
  assert.equal(r.u(4), 0, "constraint_set2..5_flag");
  assert.equal(r.u(2), 0, "reserved_zero_2bits");
  assert.equal(r.u(8), 30, "level_idc");
  assert.equal(r.ue(), 0, "seq_parameter_set_id");
  assert.equal(r.ue(), 0, "log2_max_frame_num_minus4");
  assert.equal(r.ue(), 2, "pic_order_cnt_type");
  assert.equal(r.ue(), 1, "max_num_ref_frames");
  assert.equal(r.u(1), 0, "gaps_in_frame_num_value_allowed_flag");
  assert.equal(r.ue(), WIDTH / 16 - 1, "pic_width_in_mbs_minus1");
  assert.equal(r.ue(), HEIGHT / 16 - 1, "pic_height_in_map_units_minus1");
  assert.equal(r.u(1), 1, "frame_mbs_only_flag");
  assert.equal(r.u(1), 1, "direct_8x8_inference_flag");
  assert.equal(r.u(1), 0, "frame_cropping_flag");
  assert.equal(r.u(1), 0, "vui_parameters_present_flag");
  r.trailing();
  assert.equal(codecString(sps), "avc1.42C01E");
  assert.equal(codecString(Buffer.concat([Buffer.from([0, 0, 0, 1]), sps])), "avc1.42C01E");
  assert.throws(() => codecString(encodePps()), /SPS/);
});

test("PPS fields match the contract, including deblocking_filter_control_present_flag 1", () => {
  const r = new BitReader(rbspOf(encodePps()));
  assert.equal(r.ue(), 0, "pic_parameter_set_id");
  assert.equal(r.ue(), 0, "seq_parameter_set_id");
  assert.equal(r.u(1), 0, "entropy_coding_mode_flag");
  assert.equal(r.u(1), 0, "bottom_field_pic_order_in_frame_present_flag");
  assert.equal(r.ue(), 0, "num_slice_groups_minus1");
  assert.equal(r.ue(), 0, "num_ref_idx_l0_default_active_minus1");
  assert.equal(r.ue(), 0, "num_ref_idx_l1_default_active_minus1");
  assert.equal(r.u(1), 0, "weighted_pred_flag");
  assert.equal(r.u(2), 0, "weighted_bipred_idc");
  assert.equal(r.se(), 0, "pic_init_qp_minus26");
  assert.equal(r.se(), 0, "pic_init_qs_minus26");
  assert.equal(r.se(), 0, "chroma_qp_index_offset");
  assert.equal(r.u(1), 1, "deblocking_filter_control_present_flag");
  assert.equal(r.u(1), 0, "constrained_intra_pred_flag");
  assert.equal(r.u(1), 0, "redundant_pic_cnt_present_flag");
  r.trailing();
});

function parseSlice(nal, planes) {
  const r = new BitReader(rbspOf(nal));
  const header = {
    first_mb_in_slice: r.ue(),
    slice_type: r.ue(),
    pic_parameter_set_id: r.ue(),
    frame_num: r.u(4),
    idr_pic_id: r.ue(),
    no_output_of_prior_pics_flag: r.u(1),
    long_term_reference_flag: r.u(1),
    slice_qp_delta: r.se(),
    disable_deblocking_filter_idc: r.ue(),
  };
  const mbWidth = WIDTH / 16;
  for (let mb = 0; mb < MBS; mb += 1) {
    assert.equal(r.ue(), 25, `mb_type I_PCM at macroblock ${mb}`);
    while (!r.aligned) assert.equal(r.bit(), 0, "pcm_alignment_zero_bit");
    const mbX = mb % mbWidth;
    const mbY = Math.floor(mb / mbWidth);
    for (let y = 0; y < 16; y += 1) {
      for (let x = 0; x < 16; x += 1) {
        const sample = r.u(8);
        if (planes) assert.equal(sample, planes.luma[(mbY * 16 + y) * WIDTH + mbX * 16 + x]);
        assert.ok(sample >= 16 && sample <= 235, "luma clamped to 16-235");
      }
    }
    for (const plane of ["cb", "cr"]) {
      for (let y = 0; y < 8; y += 1) {
        for (let x = 0; x < 8; x += 1) {
          const sample = r.u(8);
          if (planes) assert.equal(sample, planes[plane][(mbY * 8 + y) * (WIDTH / 2) + mbX * 8 + x]);
          assert.ok(sample >= 16 && sample <= 240, "chroma clamped to 16-240");
        }
      }
    }
  }
  r.trailing();
  return header;
}

test("IDR slice header and I_PCM slice data match the contract", () => {
  const frame = 5;
  const timeMs = 1_760_000_012_345;
  const planes = renderPattern({ width: WIDTH, height: HEIGHT, frame, timeMs });
  const nals = splitAnnexB(makeFrame({ width: WIDTH, height: HEIGHT, frame, timeMs }).annexB);
  const header = parseSlice(nals[2], planes);
  assert.deepEqual(header, {
    first_mb_in_slice: 0,
    slice_type: 7,
    pic_parameter_set_id: 0,
    frame_num: 0,
    idr_pic_id: 1,
    no_output_of_prior_pics_flag: 0,
    long_term_reference_flag: 0,
    slice_qp_delta: 0,
    disable_deblocking_filter_idc: 1,
  });
  const even = splitAnnexB(makeFrame({ width: WIDTH, height: HEIGHT, frame: 6, timeMs }).annexB);
  assert.equal(parseSlice(even[2]).idr_pic_id, 0, "idr_pic_id alternates");
});

test("emulation prevention leaves no 00 00 0x (x <= 3) inside any NAL payload", () => {
  assert.deepEqual([...addEmulationPrevention(Uint8Array.from([0, 0, 0, 0, 0, 1, 0, 0, 2, 0, 0, 3, 0, 0, 4]))], [0, 0, 3, 0, 0, 3, 0, 1, 0, 0, 3, 2, 0, 0, 3, 3, 0, 0, 4]);
  assert.deepEqual([...addEmulationPrevention(Uint8Array.from([5, 0]))], [5, 0, 3]);
  for (const frame of [0, 1, 2, 3, 4, 999_999]) {
    for (const nal of splitAnnexB(makeFrame({ frame, timeMs: frame * 7 }).annexB)) {
      for (let i = 0; i + 2 < nal.length; i += 1) {
        assert.ok(!(nal[i] === 0 && nal[i + 1] === 0 && nal[i + 2] <= 3), `start-code emulation at ${i}`);
      }
    }
  }
  // Round trip: removing the prevention bytes gives back the original RBSP.
  const raw = Uint8Array.from([1, 0, 0, 0, 0, 0, 2, 0, 0, 3, 9, 0, 0]);
  assert.deepEqual([...removeEmulationPrevention(addEmulationPrevention(raw))].slice(0, raw.length), [...raw]);
});

test("pattern is deterministic for (frame, timeMs) and the digits change the bytes", () => {
  const a = makeFrame({ frame: 42, timeMs: 1_760_000_000_123 }).annexB;
  const b = makeFrame({ frame: 42, timeMs: 1_760_000_000_123 }).annexB;
  assert.ok(a.equals(b), "same input, same bytes");
  // Frame 50 vs 42 with the bar at the same place modulo width is not needed: compare counters only.
  const counterOnly = makeFrame({ frame: 42 + WIDTH / 2, timeMs: 1_760_000_000_123 }).annexB;
  assert.ok(!a.equals(counterOnly), "frame counter digits change the frame (bar position is the same)");
  const clockOnly = makeFrame({ frame: 42, timeMs: 1_760_000_000_124 }).annexB;
  assert.ok(!a.equals(clockOnly), "server-time digits change the frame");
  const sameClock = makeFrame({ frame: 42, timeMs: 1_760_000_000_123 + 100_000 }).annexB;
  assert.ok(a.equals(sameClock), "line 2 shows timeMs % 100000 only");
  const p1 = renderPattern({ frame: 0, timeMs: 0 });
  const p2 = renderPattern({ frame: 1, timeMs: 0 });
  assert.notDeepEqual(p1.luma, p2.luma, "the bar moves");
  assert.equal(p1.luma.length, WIDTH * HEIGHT);
  assert.equal(p1.cb.length, (WIDTH / 2) * (HEIGHT / 2));
  assert.equal(p1.cr.length, (WIDTH / 2) * (HEIGHT / 2));
});

test("frame size for 96x208 is 78 PCM macroblocks plus headers", () => {
  const { annexB } = makeFrame({ frame: 1, timeMs: 1 });
  const config = configAnnexB({ width: WIDTH, height: HEIGHT });
  assert.ok(annexB.subarray(0, config.length).equals(config), "access unit starts with the config");
  const idr = splitAnnexB(annexB)[2];
  // 384 sample bytes per macroblock plus at most 2 bytes of mb_type/alignment, header and trailing bits.
  assert.ok(idr.length >= MBS * 384 && idr.length <= MBS * 386 + 32, `IDR NAL is ${idr.length} bytes`);
  assert.ok(annexB.length > 30_000 && annexB.length < 31_000, `access unit is ${annexB.length} bytes`);
  assert.throws(() => encodeSps({ width: 100, height: 208 }), /multiples of 16/);
  assert.throws(() => encodeIdrFrame({ width: 16, height: 16, luma: new Uint8Array(3), cb: new Uint8Array(64), cr: new Uint8Array(64) }), /luma/);
});

const ffprobe = spawnSync("ffprobe", ["-version"], { stdio: "ignore" });
const ffprobeMissing = ffprobe.error || ffprobe.status !== 0 ? "ffprobe is not on PATH; the real decoder check needs it" : false;

test("ffprobe decodes 10 generated frames as 96x208", { skip: ffprobeMissing }, (t) => {
  const dir = mkdtempSync(join(tmpdir(), "probe-h264-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, "pattern.h264");
  const parts = [];
  for (let frame = 0; frame < 10; frame += 1) parts.push(makeFrame({ frame, timeMs: 1_760_000_000_000 + frame * 33 }).annexB);
  writeFileSync(file, Buffer.concat(parts));
  const result = spawnSync(
    "ffprobe",
    ["-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=codec_name,profile,level,width,height,nb_read_frames", "-of", "json", file],
    { encoding: "utf8", timeout: 30_000 },
  );
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr.trim(), "", "no decode errors");
  const stream = JSON.parse(result.stdout).streams[0];
  assert.equal(stream.codec_name, "h264");
  assert.equal(stream.width, 96);
  assert.equal(stream.height, 208);
  assert.equal(Number(stream.nb_read_frames), 10);
  assert.equal(stream.level, 30);
  assert.match(stream.profile, /Baseline/);
});
