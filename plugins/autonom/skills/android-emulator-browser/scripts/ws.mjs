/**
 * Minimal RFC 6455 WebSocket server side for the Canvas (no extensions, no client
 * role). Callers authenticate the upgrade request before calling acceptWebSocket.
 * Server frames are never masked and never fragmented. Exports are part of the
 * test and server contracts — keep names stable.
 */
import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";

export const WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
export const DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024;
// How long a closing connection may wait for the peer before the socket is destroyed.
export const CLOSE_TIMEOUT_MS = 2000;

export const CLOSE_CODE = Object.freeze({
  NORMAL: 1000,
  GOING_AWAY: 1001,
  PROTOCOL_ERROR: 1002,
  UNSUPPORTED_DATA: 1003,
  NO_STATUS: 1005,
  ABNORMAL: 1006,
  INVALID_PAYLOAD: 1007,
  POLICY_VIOLATION: 1008,
  MESSAGE_TOO_BIG: 1009,
  INTERNAL_ERROR: 1011,
});

export const READY_STATE = Object.freeze({ OPEN: 1, CLOSING: 2, CLOSED: 3 });

const OPCODE = Object.freeze({
  CONTINUATION: 0x0,
  TEXT: 0x1,
  BINARY: 0x2,
  CLOSE: 0x8,
  PING: 0x9,
  PONG: 0xa,
});

const STATUS_TEXT = {
  400: "Bad Request",
  401: "Unauthorized",
  403: "Forbidden",
  404: "Not Found",
  426: "Upgrade Required",
  503: "Service Unavailable",
};

const CLOSE_REASON_MAX_BYTES = 123;
const WEBSOCKET_KEY = /^[A-Za-z0-9+/]{22}==$/;
const UTF8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const EMPTY = Buffer.alloc(0);

function noop() {}

function headerTokens(value) {
  return String(value ?? "")
    .toLowerCase()
    .split(",")
    .map((token) => token.trim())
    .filter(Boolean);
}

export function websocketAcceptKey(key) {
  return createHash("sha1").update(`${key}${WEBSOCKET_GUID}`).digest("base64");
}

/** Answer an upgrade request with a plain HTTP error and close the socket. */
export function rejectUpgrade(socket, status, message, headers = {}) {
  if (socket.destroyed) return;
  socket.on("error", noop);
  const statusText = STATUS_TEXT[status] ?? "Error";
  const body = `${message ?? statusText}\n`;
  const lines = [
    `HTTP/1.1 ${status} ${statusText}`,
    "Connection: close",
    "Content-Type: text/plain; charset=utf-8",
    `Content-Length: ${Buffer.byteLength(body)}`,
    ...Object.entries(headers).map(([name, value]) => `${name}: ${value}`),
  ];
  socket.end(`${lines.join("\r\n")}\r\n\r\n${body}`);
  setTimeout(() => socket.destroy(), CLOSE_TIMEOUT_MS).unref?.();
}

function handshakeProblem(request) {
  const headers = request.headers ?? {};
  if (request.method !== "GET") return { status: 400, message: "WebSocket upgrades must use GET." };
  if (!headerTokens(headers.upgrade).includes("websocket")) {
    return { status: 400, message: "Missing Upgrade: websocket." };
  }
  if (!headerTokens(headers.connection).includes("upgrade")) {
    return { status: 400, message: "Missing Connection: Upgrade." };
  }
  if (headers["sec-websocket-version"] !== "13") {
    return {
      status: 426,
      message: "Only WebSocket version 13 is supported.",
      headers: { "Sec-WebSocket-Version": "13" },
    };
  }
  const key = headers["sec-websocket-key"];
  if (typeof key !== "string" || !WEBSOCKET_KEY.test(key.trim())) {
    return { status: 400, message: "Invalid Sec-WebSocket-Key." };
  }
  return null;
}

function chooseProtocol(offered, supported = []) {
  const wanted = String(offered ?? "")
    .split(",")
    .map((token) => token.trim())
    .filter(Boolean);
  if (!Array.isArray(supported)) return null;
  return wanted.find((name) => supported.includes(name)) ?? null;
}

function isValidReceivedCloseCode(code) {
  return (
    (code >= 1000 && code <= 1003) ||
    (code >= 1007 && code <= 1014) ||
    (code >= 3000 && code <= 4999)
  );
}

function isSendableCloseCode(code) {
  return Number.isInteger(code) && isValidReceivedCloseCode(code);
}

function truncateUtf8(text, maximumBytes) {
  const bytes = Buffer.from(String(text ?? ""), "utf8");
  if (bytes.length <= maximumBytes) return bytes;
  let end = maximumBytes;
  // Step back over continuation bytes so a multi-byte character is never split.
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end -= 1;
  return bytes.subarray(0, end);
}

function frameHeader(opcode, length) {
  if (length < 126) return Buffer.from([0x80 | opcode, length]);
  if (length <= 0xffff) {
    const header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(length, 2);
    return header;
  }
  const header = Buffer.alloc(10);
  header[0] = 0x80 | opcode;
  header[1] = 127;
  header.writeBigUInt64BE(BigInt(length), 2);
  return header;
}

function unmaskInto(masked, key, target, offset) {
  for (let i = 0; i < masked.length; i += 1) target[offset + i] = masked[i] ^ key[i & 3];
}

function unmask(masked, key) {
  const out = Buffer.allocUnsafe(masked.length);
  unmaskInto(masked, key, out, 0);
  return out;
}

function toPayload(data) {
  if (Buffer.isBuffer(data)) return data;
  if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  if (data instanceof ArrayBuffer) return Buffer.from(data);
  throw new TypeError("send() takes a string, Buffer, TypedArray or ArrayBuffer.");
}

class WebSocketConnection extends EventEmitter {
  #socket;
  #maxMessageBytes;
  // Bytes being parsed by the current #receive call.
  #buffer = EMPTY;
  // An incomplete frame waits here; #needBytes says how many bytes parsing needs next.
  #pending = EMPTY;
  #pendingLength = 0;
  #needBytes = 2;
  // A fragmented message is assembled in one buffer that never grows past maxMessageBytes.
  #message = null;
  #messageBytes = 0;
  #messageOpcode = 0;
  #pendingPong = null;
  #readyState = READY_STATE.OPEN;
  #closeSent = false;
  #discardInput = false;
  #closeCode = null;
  #closeReason = "";
  #closeTimer = null;

  constructor(socket, { maxMessageBytes, protocol, head }) {
    super();
    this.#socket = socket;
    this.#maxMessageBytes = maxMessageBytes;
    this.protocol = protocol;
    const early = head?.length && !socket.destroyed ? Buffer.from(head) : null;
    // When the upgrade is accepted late (for example after async authorization), the
    // socket may already hold bytes sent after the request, or have seen the peer's FIN.
    const peerEnded = socket.readableEnded;
    // Unshift before the "data" listener starts the flow, so the early bytes are parsed
    // first, after the caller has attached its listeners.
    if (early && !peerEnded) socket.unshift(early);
    socket.setNoDelay?.(true);
    socket.setTimeout?.(0);
    socket.on("data", (chunk) => this.#receive(chunk));
    socket.on("drain", () => this.#flushPong());
    socket.on("error", noop);
    // HTTP server sockets allow half-open connections; finish our side when the peer ends.
    socket.on("end", () => socket.end());
    socket.on("close", () => this.#finish());
    if (socket.destroyed) {
      // The peer left before the upgrade was accepted, so "close" already fired on the
      // socket; report it once here or callers would count a client that is gone.
      process.nextTick(() => this.#finish());
    } else if (peerEnded) {
      // "end" already fired, so the handler above never runs and a stream cannot take
      // bytes back after it: parse the early bytes here, then finish our side as on "end".
      process.nextTick(() => {
        if (early) this.#receive(early);
        socket.end();
      });
    }
  }

  get bufferedAmount() {
    return this.#socket.writableLength;
  }

  /** True while the socket owes a "drain" event: a write went past its high-water mark. */
  get writableNeedDrain() {
    return this.#socket.writableNeedDrain === true;
  }

  get readyState() {
    return this.#readyState;
  }

  send(data) {
    if (this.#readyState !== READY_STATE.OPEN || !this.#writable()) return false;
    // A pong held back by backpressure goes out with the caller's next message.
    this.#flushPong();
    if (typeof data === "string") return this.#writeFrame(OPCODE.TEXT, Buffer.from(data, "utf8"));
    return this.#writeFrame(OPCODE.BINARY, toPayload(data));
  }

  close(code = CLOSE_CODE.NORMAL, reason = "") {
    if (!isSendableCloseCode(code)) {
      throw new RangeError(`Close code ${code} cannot be sent.`);
    }
    if (this.#closeSent || this.#readyState === READY_STATE.CLOSED) return;
    this.#closeCode = code;
    this.#closeReason = truncateUtf8(reason, CLOSE_REASON_MAX_BYTES).toString("utf8");
    this.#sendClose(code, this.#closeReason);
    this.#armCloseTimer();
  }

  #receive(chunk) {
    if (this.#readyState === READY_STATE.CLOSED || this.#discardInput || !chunk.length) return;
    let buffer = chunk;
    if (this.#pendingLength) {
      this.#keep(chunk);
      // Parse only once the waiting frame is complete, so a frame that trickles in is copied once.
      if (this.#pendingLength < this.#needBytes) return;
      buffer = this.#pending.subarray(0, this.#pendingLength);
    }
    this.#buffer = buffer;
    while (!this.#discardInput && this.#readFrame()) {
      // keep parsing complete frames
    }
    const rest = this.#buffer;
    this.#buffer = EMPTY;
    this.#pendingLength = 0;
    if (!rest.length || this.#discardInput || this.#readyState === READY_STATE.CLOSED) {
      this.#pending = EMPTY;
      return;
    }
    this.#keep(rest);
  }

  /** Append bytes of an incomplete frame; the store is sized for the whole frame once its header is known. */
  #keep(bytes) {
    const length = this.#pendingLength + bytes.length;
    if (this.#pending.length < length) {
      const grown = Buffer.allocUnsafe(Math.max(length, this.#needBytes));
      this.#pending.copy(grown, 0, 0, this.#pendingLength);
      this.#pending = grown;
    }
    // Buffer#copy handles the overlap when `bytes` is the tail of #pending itself.
    bytes.copy(this.#pending, this.#pendingLength);
    this.#pendingLength = length;
  }

  #needMore(bytes) {
    this.#needBytes = bytes;
    return false;
  }

  #writable() {
    return !this.#socket.destroyed && this.#socket.writable !== false;
  }

  #writeFrame(opcode, payload) {
    if (!this.#writable()) return false;
    const header = frameHeader(opcode, payload.length);
    this.#socket.cork?.();
    let accepted = this.#socket.write(header);
    if (payload.length) accepted = this.#socket.write(payload);
    this.#socket.uncork?.();
    return accepted;
  }

  #sendClose(code, reason) {
    const payload =
      code == null
        ? EMPTY
        : Buffer.concat([Buffer.from([code >> 8, code & 0xff]), Buffer.from(reason, "utf8")]);
    this.#writeFrame(OPCODE.CLOSE, payload);
    this.#closeSent = true;
    this.#pendingPong = null;
    this.#readyState = READY_STATE.CLOSING;
  }

  #flushPong() {
    const payload = this.#pendingPong;
    this.#pendingPong = null;
    if (payload && !this.#closeSent) this.#writeFrame(OPCODE.PONG, payload);
  }

  #answerPing(payload) {
    // RFC 6455 section 5.5.3 lets an endpoint answer only the most recent ping. While the
    // peer is not reading, keep one pending pong instead of growing the send buffer.
    if (this.#socket.writableNeedDrain) {
      this.#pendingPong = payload;
      return;
    }
    this.#writeFrame(OPCODE.PONG, payload);
  }

  #dropInput() {
    this.#buffer = EMPTY;
    this.#pending = EMPTY;
    this.#pendingLength = 0;
    this.#resetMessage();
  }

  #resetMessage() {
    this.#message = null;
    this.#messageBytes = 0;
    this.#messageOpcode = 0;
  }

  #armCloseTimer() {
    if (this.#closeTimer) return;
    this.#closeTimer = setTimeout(() => this.#socket.destroy(), CLOSE_TIMEOUT_MS);
    this.#closeTimer.unref?.();
  }

  #fail(code, reason) {
    if (!this.#closeSent) {
      this.#closeCode = code;
      this.#closeReason = reason;
      this.#sendClose(code, reason);
    }
    // The rest of a refused frame may still be arriving; keep reading and dropping it
    // so the peer receives the close frame instead of a connection reset.
    this.#discardInput = true;
    this.#dropInput();
    this.#socket.end();
    this.#armCloseTimer();
    return false;
  }

  #finish() {
    if (this.#readyState === READY_STATE.CLOSED) return;
    this.#readyState = READY_STATE.CLOSED;
    clearTimeout(this.#closeTimer);
    this.#dropInput();
    this.#pendingPong = null;
    this.emit("close", this.#closeCode ?? CLOSE_CODE.ABNORMAL, this.#closeReason);
  }

  #readFrame() {
    const buffer = this.#buffer;
    if (buffer.length < 2) return this.#needMore(2);
    const fin = (buffer[0] & 0x80) !== 0;
    const opcode = buffer[0] & 0x0f;
    const hasMask = (buffer[1] & 0x80) !== 0;
    let length = buffer[1] & 0x7f;

    if (buffer[0] & 0x70) return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Reserved bits are set.");
    if (!hasMask) return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Client frames must be masked.");
    if (opcode >= OPCODE.CLOSE) {
      if (opcode > OPCODE.PONG) return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Unknown opcode.");
      if (!fin || length > 125) {
        return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Control frames must be short and unfragmented.");
      }
    } else if (opcode === OPCODE.CONTINUATION) {
      if (!this.#messageOpcode) return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Unexpected continuation frame.");
    } else if (opcode === OPCODE.TEXT || opcode === OPCODE.BINARY) {
      if (this.#messageOpcode) return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Expected a continuation frame.");
    } else {
      return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Unknown opcode.");
    }

    let offset = 2;
    if (length === 126) {
      if (buffer.length < 4) return this.#needMore(4);
      length = buffer.readUInt16BE(2);
      offset = 4;
    } else if (length === 127) {
      if (buffer.length < 10) return this.#needMore(10);
      const declared = buffer.readBigUInt64BE(2);
      if (declared >> 63n) return this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Invalid payload length.");
      if (declared > BigInt(this.#maxMessageBytes)) {
        return this.#fail(CLOSE_CODE.MESSAGE_TOO_BIG, "Message is too big.");
      }
      length = Number(declared);
      offset = 10;
    }
    // Refuse oversize messages from the header so their payload is never buffered.
    if (opcode < OPCODE.CLOSE && this.#messageBytes + length > this.#maxMessageBytes) {
      return this.#fail(CLOSE_CODE.MESSAGE_TOO_BIG, "Message is too big.");
    }
    const end = offset + 4 + length;
    if (buffer.length < end) return this.#needMore(end);

    const key = buffer.subarray(offset, offset + 4);
    const masked = buffer.subarray(offset + 4, end);
    this.#buffer = buffer.subarray(end);
    this.#handleFrame(fin, opcode, masked, key);
    return this.#readyState !== READY_STATE.CLOSED;
  }

  /** `masked` may point into reused input memory, so every path unmasks into its own buffer. */
  #handleFrame(fin, opcode, masked, key) {
    if (opcode === OPCODE.CLOSE) {
      this.#handleClose(unmask(masked, key));
      return;
    }
    // After our close frame only the peer's close frame matters.
    if (this.#closeSent) return;
    if (opcode === OPCODE.PING) {
      this.#answerPing(unmask(masked, key));
      return;
    }
    if (opcode === OPCODE.PONG) return;

    if (opcode !== OPCODE.CONTINUATION) this.#messageOpcode = opcode;
    let data;
    if (fin && this.#messageBytes === 0) {
      data = unmask(masked, key);
    } else {
      this.#appendFragment(masked, key);
      if (!fin) return;
      data = this.#message.subarray(0, this.#messageBytes);
    }
    const isBinary = this.#messageOpcode === OPCODE.BINARY;
    this.#resetMessage();
    if (isBinary) {
      this.emit("message", data, true);
      return;
    }
    let text;
    try {
      text = UTF8.decode(data);
    } catch {
      this.#fail(CLOSE_CODE.INVALID_PAYLOAD, "Text is not valid UTF-8.");
      return;
    }
    this.emit("message", text, false);
  }

  #appendFragment(masked, key) {
    // Empty fragments add nothing, so storing them would let a peer grow memory for free.
    if (!masked.length) return;
    // At most maxMessageBytes: #readFrame refuses larger totals from the frame header.
    const length = this.#messageBytes + masked.length;
    if (!this.#message || this.#message.length < length) {
      const doubled = (this.#message?.length ?? 0) * 2;
      const grown = Buffer.allocUnsafe(Math.min(this.#maxMessageBytes, Math.max(length, doubled)));
      this.#message?.copy(grown, 0, 0, this.#messageBytes);
      this.#message = grown;
    }
    unmaskInto(masked, key, this.#message, this.#messageBytes);
    this.#messageBytes = length;
  }

  #handleClose(payload) {
    if (this.#closeSent) {
      // The peer answered our close frame: the handshake is complete.
      this.#discardInput = true;
      this.#socket.end();
      return;
    }
    let code = CLOSE_CODE.NO_STATUS;
    let reason = "";
    if (payload.length === 1) {
      this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Close frame payload is too short.");
      return;
    }
    if (payload.length >= 2) {
      code = payload.readUInt16BE(0);
      if (!isValidReceivedCloseCode(code)) {
        this.#fail(CLOSE_CODE.PROTOCOL_ERROR, "Invalid close code.");
        return;
      }
      try {
        reason = UTF8.decode(payload.subarray(2));
      } catch {
        this.#fail(CLOSE_CODE.INVALID_PAYLOAD, "Close reason is not valid UTF-8.");
        return;
      }
    }
    this.#closeCode = code;
    this.#closeReason = reason;
    this.#sendClose(code === CLOSE_CODE.NO_STATUS ? null : code, "");
    this.#discardInput = true;
    // RFC 6455 section 7.1.1: the server closes the TCP connection first.
    this.#socket.end();
    this.#armCloseTimer();
  }
}

/**
 * Complete a WebSocket upgrade on `socket` (from the HTTP server's `upgrade` event).
 * Returns the connection, or null after answering an invalid handshake with 400/426.
 * Bytes in `head` are parsed after the caller has attached its listeners and before any
 * bytes that reached the socket later, so the accept may wait on async authorization.
 */
export function acceptWebSocket(request, socket, head, { maxMessageBytes = DEFAULT_MAX_MESSAGE_BYTES, protocols = [] } = {}) {
  if (!Number.isInteger(maxMessageBytes) || maxMessageBytes < 1) {
    throw new RangeError("maxMessageBytes must be a positive integer.");
  }
  const problem = handshakeProblem(request);
  if (problem) {
    rejectUpgrade(socket, problem.status, problem.message, problem.headers);
    return null;
  }
  const protocol = chooseProtocol(request.headers["sec-websocket-protocol"], protocols);
  const lines = [
    "HTTP/1.1 101 Switching Protocols",
    "Upgrade: websocket",
    "Connection: Upgrade",
    `Sec-WebSocket-Accept: ${websocketAcceptKey(request.headers["sec-websocket-key"].trim())}`,
  ];
  if (protocol) lines.push(`Sec-WebSocket-Protocol: ${protocol}`);
  if (!socket.destroyed) socket.write(`${lines.join("\r\n")}\r\n\r\n`);

  return new WebSocketConnection(socket, { maxMessageBytes, protocol, head });
}
