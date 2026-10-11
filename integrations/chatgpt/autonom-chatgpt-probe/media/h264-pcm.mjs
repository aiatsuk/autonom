/**
 * Generated H.264 test pattern for the Autonom ChatGPT probe (stdlib only).
 *
 * Every access unit is SPS + PPS + one IDR slice whose macroblocks are all I_PCM, so the
 * stream needs no real encoder: Baseline profile (constraint_set0/1 → avc1.42C01E), level
 * 3.0, CAVLC, pic_order_cnt_type 2, deblocking disabled. Field values follow ITU-T H.264
 * 7.3.2.1 (SPS), 7.3.2.2 (PPS), 7.3.3 (slice header) and 7.3.5 (macroblock layer).
 */

export const DEFAULT_WIDTH = 96;
export const DEFAULT_HEIGHT = 208;
export const START_CODE = Object.freeze([0, 0, 0, 1]);

const NAL_SPS = 7;
const NAL_PPS = 8;
const NAL_IDR = 5;
const NAL_REF_IDC = 3;

const LUMA_MIN = 16;
const LUMA_MAX = 235;
const CHROMA_MIN = 16;
const CHROMA_MAX = 240;

/** Big-endian bit writer over a growable byte buffer (RBSP, before emulation prevention). */
class BitWriter {
  #bytes;
  #length = 0;
  #current = 0;
  #bits = 0;

  constructor(capacity = 64) {
    this.#bytes = new Uint8Array(Math.max(16, capacity));
  }

  #ensure(extra) {
    if (this.#length + extra <= this.#bytes.length) return;
    const next = new Uint8Array(Math.max(this.#bytes.length * 2, this.#length + extra + 16));
    next.set(this.#bytes.subarray(0, this.#length));
    this.#bytes = next;
  }

  bit(value) {
    this.#current = (this.#current << 1) | (value ? 1 : 0);
    this.#bits += 1;
    if (this.#bits === 8) {
      this.#ensure(1);
      this.#bytes[this.#length] = this.#current;
      this.#length += 1;
      this.#current = 0;
      this.#bits = 0;
    }
  }

  /** u(n): unsigned integer in n bits, most significant bit first. */
  u(count, value) {
    for (let shift = count - 1; shift >= 0; shift -= 1) this.bit((value >>> shift) & 1);
  }

  /** ue(v): unsigned Exp-Golomb. */
  ue(value) {
    if (!Number.isInteger(value) || value < 0) throw new RangeError(`ue(v) needs a non-negative integer, got ${value}`);
    const code = value + 1;
    const length = 31 - Math.clz32(code);
    this.u(length, 0);
    this.u(length + 1, code);
  }

  /** se(v): signed Exp-Golomb. */
  se(value) {
    if (!Number.isInteger(value)) throw new RangeError(`se(v) needs an integer, got ${value}`);
    this.ue(value <= 0 ? -2 * value : 2 * value - 1);
  }

  get aligned() {
    return this.#bits === 0;
  }

  alignZero() {
    while (this.#bits !== 0) this.bit(0);
  }

  /** Copy whole bytes; the writer must be byte aligned. */
  bytes(source) {
    if (this.#bits !== 0) throw new Error("BitWriter.bytes() needs byte alignment");
    this.#ensure(source.length);
    this.#bytes.set(source, this.#length);
    this.#length += source.length;
  }

  /** rbsp_trailing_bits: one stop bit, then zero bits to the byte boundary. */
  trailing() {
    this.bit(1);
    this.alignZero();
  }

  finish() {
    if (this.#bits !== 0) throw new Error("BitWriter.finish() needs byte alignment");
    return this.#bytes.slice(0, this.#length);
  }
}

/** Insert emulation_prevention_three_byte after every 00 00 that precedes a byte <= 3. */
export function addEmulationPrevention(buf) {
  const input = buf instanceof Uint8Array ? buf : Uint8Array.from(buf);
  const out = new Uint8Array(input.length + Math.ceil(input.length / 2) + 1);
  let length = 0;
  let zeros = 0;
  for (let i = 0; i < input.length; i += 1) {
    const byte = input[i];
    if (zeros >= 2 && byte <= 3) {
      out[length] = 3;
      length += 1;
      zeros = 0;
    }
    out[length] = byte;
    length += 1;
    zeros = byte === 0 ? zeros + 1 : 0;
  }
  // A payload may not end in 00 (7.4.1): append 03 after a trailing zero byte.
  if (length && out[length - 1] === 0) {
    out[length] = 3;
    length += 1;
  }
  return Buffer.from(out.buffer, out.byteOffset, length);
}

function nalUnit(type, rbsp) {
  const payload = addEmulationPrevention(rbsp);
  const header = Buffer.from([(NAL_REF_IDC << 5) | type]);
  return Buffer.concat([header, payload]);
}

function checkSize(width, height) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 16 || height < 16 || width % 16 || height % 16) {
    throw new RangeError(`Frame size must be positive multiples of 16, got ${width}x${height}`);
  }
}

/** Sequence parameter set NAL unit (header byte included, no start code). */
export function encodeSps({ width = DEFAULT_WIDTH, height = DEFAULT_HEIGHT } = {}) {
  checkSize(width, height);
  const w = new BitWriter(32);
  w.u(8, 66); // profile_idc: Baseline
  w.u(1, 1); // constraint_set0_flag
  w.u(1, 1); // constraint_set1_flag
  w.u(4, 0); // constraint_set2..5_flag
  w.u(2, 0); // reserved_zero_2bits
  w.u(8, 30); // level_idc 3.0
  w.ue(0); // seq_parameter_set_id
  w.ue(0); // log2_max_frame_num_minus4
  w.ue(2); // pic_order_cnt_type
  w.ue(1); // max_num_ref_frames
  w.u(1, 0); // gaps_in_frame_num_value_allowed_flag
  w.ue(width / 16 - 1); // pic_width_in_mbs_minus1
  w.ue(height / 16 - 1); // pic_height_in_map_units_minus1
  w.u(1, 1); // frame_mbs_only_flag
  w.u(1, 1); // direct_8x8_inference_flag
  w.u(1, 0); // frame_cropping_flag
  w.u(1, 0); // vui_parameters_present_flag
  w.trailing();
  return nalUnit(NAL_SPS, w.finish());
}

/** Picture parameter set NAL unit (header byte included, no start code). */
export function encodePps() {
  const w = new BitWriter(16);
  w.ue(0); // pic_parameter_set_id
  w.ue(0); // seq_parameter_set_id
  w.u(1, 0); // entropy_coding_mode_flag: CAVLC
  w.u(1, 0); // bottom_field_pic_order_in_frame_present_flag
  w.ue(0); // num_slice_groups_minus1
  w.ue(0); // num_ref_idx_l0_default_active_minus1
  w.ue(0); // num_ref_idx_l1_default_active_minus1
  w.u(1, 0); // weighted_pred_flag
  w.u(2, 0); // weighted_bipred_idc
  w.se(0); // pic_init_qp_minus26
  w.se(0); // pic_init_qs_minus26
  w.se(0); // chroma_qp_index_offset
  w.u(1, 1); // deblocking_filter_control_present_flag
  w.u(1, 0); // constrained_intra_pred_flag
  w.u(1, 0); // redundant_pic_cnt_present_flag
  w.trailing();
  return nalUnit(NAL_PPS, w.finish());
}

/** One IDR slice NAL unit (header byte included, no start code) with every macroblock I_PCM. */
export function encodeIdrFrame({ width = DEFAULT_WIDTH, height = DEFAULT_HEIGHT, luma, cb, cr, idrPicId = 0 }) {
  checkSize(width, height);
  const chromaWidth = width / 2;
  const chromaHeight = height / 2;
  if (!luma || luma.length !== width * height) throw new RangeError("luma plane has the wrong size");
  if (!cb || cb.length !== chromaWidth * chromaHeight) throw new RangeError("cb plane has the wrong size");
  if (!cr || cr.length !== chromaWidth * chromaHeight) throw new RangeError("cr plane has the wrong size");
  if (idrPicId !== 0 && idrPicId !== 1) throw new RangeError("idrPicId must be 0 or 1");
  const mbWidth = width / 16;
  const mbHeight = height / 16;
  const w = new BitWriter(mbWidth * mbHeight * 388 + 32);
  // Slice header (7.3.3), IDR, I slice.
  w.ue(0); // first_mb_in_slice
  w.ue(7); // slice_type: I (all slices of the picture are I)
  w.ue(0); // pic_parameter_set_id
  w.u(4, 0); // frame_num, log2_max_frame_num = 4
  w.ue(idrPicId); // idr_pic_id
  // pic_order_cnt_type 2: no POC fields. dec_ref_pic_marking() for an IDR:
  w.u(1, 0); // no_output_of_prior_pics_flag
  w.u(1, 0); // long_term_reference_flag
  w.se(0); // slice_qp_delta
  w.ue(1); // disable_deblocking_filter_idc
  // Slice data (7.3.4), CAVLC I slice: no mb_skip_run.
  const block = new Uint8Array(384);
  for (let mbY = 0; mbY < mbHeight; mbY += 1) {
    for (let mbX = 0; mbX < mbWidth; mbX += 1) {
      w.ue(25); // mb_type I_PCM
      w.alignZero(); // pcm_alignment_zero_bit, counted on the RBSP
      let k = 0;
      for (let y = 0; y < 16; y += 1) {
        const row = (mbY * 16 + y) * width + mbX * 16;
        for (let x = 0; x < 16; x += 1) block[k++] = luma[row + x];
      }
      for (const plane of [cb, cr]) {
        for (let y = 0; y < 8; y += 1) {
          const row = (mbY * 8 + y) * chromaWidth + mbX * 8;
          for (let x = 0; x < 8; x += 1) block[k++] = plane[row + x];
        }
      }
      w.bytes(block);
    }
  }
  w.trailing(); // rbsp_slice_trailing_bits
  return nalUnit(NAL_IDR, w.finish());
}

function withStartCodes(nals) {
  const parts = [];
  for (const nal of nals) parts.push(Buffer.from(START_CODE), nal);
  return Buffer.concat(parts);
}

/** SPS + PPS in Annex B (for the WebSocket `config` message and `config_b64`). */
export function configAnnexB({ width = DEFAULT_WIDTH, height = DEFAULT_HEIGHT } = {}) {
  return withStartCodes([encodeSps({ width, height }), encodePps()]);
}

/** `avc1.PPCCLL` from SPS bytes 1-3 (profile_idc, constraint flags, level_idc), hex upper. */
export function codecString(spsNal) {
  let sps = Buffer.from(spsNal);
  if (sps.length >= 4 && sps[0] === 0 && sps[1] === 0 && sps[2] === 0 && sps[3] === 1) sps = sps.subarray(4);
  else if (sps.length >= 3 && sps[0] === 0 && sps[1] === 0 && sps[2] === 1) sps = sps.subarray(3);
  if (sps.length < 4 || (sps[0] & 0x1f) !== NAL_SPS) throw new RangeError("codecString needs an SPS NAL unit");
  const hex = (byte) => byte.toString(16).toUpperCase().padStart(2, "0");
  return `avc1.${hex(sps[1])}${hex(sps[2])}${hex(sps[3])}`;
}

// 3x5 digit glyphs, one string per row, "1" = ink.
const GLYPHS = Object.freeze({
  0: ["111", "101", "101", "101", "111"],
  1: ["010", "110", "010", "010", "111"],
  2: ["111", "001", "111", "100", "111"],
  3: ["111", "001", "111", "001", "111"],
  4: ["101", "101", "111", "001", "001"],
  5: ["111", "100", "111", "001", "111"],
  6: ["111", "100", "111", "101", "111"],
  7: ["111", "001", "001", "001", "001"],
  8: ["111", "101", "111", "101", "111"],
  9: ["111", "101", "111", "001", "111"],
});
const DIGIT_SCALE = 2;
const DIGIT_PITCH = 3 * DIGIT_SCALE + 2;
const TEXT_X = 4;
const LINE_Y = Object.freeze([6, 22]);
const BAR_WIDTH = 6;

function clamp(value, low, high) {
  return value < low ? low : value > high ? high : value;
}

function fillRect(plane, stride, x0, y0, w, h, value, maxX, maxY) {
  for (let y = Math.max(0, y0); y < Math.min(maxY, y0 + h); y += 1) {
    for (let x = Math.max(0, x0); x < Math.min(maxX, x0 + w); x += 1) plane[y * stride + x] = value;
  }
}

function drawText(luma, width, height, text, x0, y0) {
  // Light backing box so the digits read on any background.
  fillRect(luma, width, x0 - 2, y0 - 2, text.length * DIGIT_PITCH + 2, 5 * DIGIT_SCALE + 4, LUMA_MAX, width, height);
  for (let i = 0; i < text.length; i += 1) {
    const glyph = GLYPHS[text[i]];
    const gx = x0 + i * DIGIT_PITCH;
    for (let row = 0; row < 5; row += 1) {
      for (let col = 0; col < 3; col += 1) {
        if (glyph[row][col] === "1") {
          fillRect(luma, width, gx + col * DIGIT_SCALE, y0 + row * DIGIT_SCALE, DIGIT_SCALE, DIGIT_SCALE, LUMA_MIN, width, height);
        }
      }
    }
  }
}

/**
 * Deterministic test picture for `(frame, timeMs)`: gradient background, a vertical bar
 * moving 2 px per frame, and burnt-in digits (line 1: frame counter, 6 digits; line 2:
 * timeMs % 100000, 5 digits). Planes are 4:2:0 and clamped to video range.
 */
export function renderPattern({ width = DEFAULT_WIDTH, height = DEFAULT_HEIGHT, frame = 0, timeMs = 0 } = {}) {
  checkSize(width, height);
  const chromaWidth = width / 2;
  const chromaHeight = height / 2;
  const luma = new Uint8Array(width * height);
  const cb = new Uint8Array(chromaWidth * chromaHeight);
  const cr = new Uint8Array(chromaWidth * chromaHeight);
  const frameIndex = Math.max(0, Math.floor(Number(frame) || 0));
  const barX = (frameIndex * 2) % width;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const onBar = x >= barX && x < barX + BAR_WIDTH;
      const value = onBar ? LUMA_MAX : 40 + Math.floor((x * 50) / width) + Math.floor((y * 120) / height);
      luma[y * width + x] = clamp(value, LUMA_MIN, LUMA_MAX);
    }
  }
  for (let y = 0; y < chromaHeight; y += 1) {
    for (let x = 0; x < chromaWidth; x += 1) {
      const onBar = x * 2 + 1 >= barX && x * 2 < barX + BAR_WIDTH;
      cb[y * chromaWidth + x] = clamp(onBar ? 200 : 96 + Math.floor((x * 64) / chromaWidth), CHROMA_MIN, CHROMA_MAX);
      cr[y * chromaWidth + x] = clamp(onBar ? 60 : 160 - Math.floor((y * 64) / chromaHeight), CHROMA_MIN, CHROMA_MAX);
    }
  }
  const counter = String(frameIndex % 1_000_000).padStart(6, "0");
  const clock = String(Math.floor(Math.abs(Number(timeMs) || 0)) % 100_000).padStart(5, "0");
  const lines = [counter, clock];
  for (let i = 0; i < lines.length; i += 1) {
    drawText(luma, width, height, lines[i], TEXT_X, LINE_Y[i]);
    // The text box is neutral grey in chroma.
    const boxX = (TEXT_X - 2) >> 1;
    const boxY = (LINE_Y[i] - 2) >> 1;
    const boxW = (lines[i].length * DIGIT_PITCH + 2) >> 1;
    const boxH = (5 * DIGIT_SCALE + 4) >> 1;
    fillRect(cb, chromaWidth, boxX, boxY, boxW, boxH, 128, chromaWidth, chromaHeight);
    fillRect(cr, chromaWidth, boxX, boxY, boxW, boxH, 128, chromaWidth, chromaHeight);
  }
  return { luma, cb, cr };
}

/** One complete access unit (SPS + PPS + IDR) in Annex B; every frame is a key frame. */
export function makeFrame({ width = DEFAULT_WIDTH, height = DEFAULT_HEIGHT, frame = 0, timeMs = 0 } = {}) {
  const planes = renderPattern({ width, height, frame, timeMs });
  const idrPicId = Math.max(0, Math.floor(Number(frame) || 0)) % 2;
  const idr = encodeIdrFrame({ width, height, ...planes, idrPicId });
  return { annexB: withStartCodes([encodeSps({ width, height }), encodePps(), idr]), key: true };
}

export const FIXTURE_CODEC = codecString(encodeSps({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT }));
