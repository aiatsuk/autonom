import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { connect } from "node:net";
import { Duplex } from "node:stream";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { setFlagsFromString } from "node:v8";
import { runInNewContext } from "node:vm";

import {
  CLOSE_CODE,
  DEFAULT_MAX_MESSAGE_BYTES,
  READY_STATE,
  acceptWebSocket,
  rejectUpgrade,
  websocketAcceptKey,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ws.mjs";

// RFC 6455 section 1.3 sample handshake.
const SAMPLE_KEY = "dGhlIHNhbXBsZSBub25jZQ==";
const SAMPLE_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=";
const OP = { CONTINUATION: 0x0, TEXT: 0x1, BINARY: 0x2, CLOSE: 0x8, PING: 0x9, PONG: 0xa };
const TIMEOUT = { timeout: 10_000 };

function echo(ws) {
  ws.on("message", (data) => ws.send(data));
}

async function startServer(t, { onConnection = echo, options, beforeAccept } = {}) {
  const sockets = new Set();
  const connections = [];
  let notify = null;
  const server = createServer((request, response) => {
    response.writeHead(404).end();
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  server.on("upgrade", async (request, socket, head) => {
    // Stands in for async authorization: without it the accept stays synchronous.
    if (beforeAccept) await beforeAccept(request, socket);
    const ws = acceptWebSocket(request, socket, head, options);
    if (!ws) return;
    const record = { ws, messages: [] };
    record.closed = new Promise((resolve) => {
      ws.once("close", (code, reason) => resolve({ code, reason }));
    });
    ws.on("message", (data, isBinary) => record.messages.push({ data, isBinary }));
    connections.push(record);
    onConnection(ws, record);
    notify?.();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
  });
  return {
    port: server.address().port,
    async connectionAt(index) {
      while (!connections[index]) await new Promise((resolve) => { notify = resolve; });
      return connections[index];
    },
  };
}

function byteReader(socket) {
  let buffer = Buffer.alloc(0);
  let ended = false;
  const waiters = [];
  const pump = () => {
    while (waiters.length) {
      const waiter = waiters[0];
      const size = waiter.size(buffer);
      if (size == null) {
        if (!ended) return;
        waiters.shift();
        waiter.reject(new Error("socket ended"));
        continue;
      }
      waiters.shift();
      const out = buffer.subarray(0, size);
      buffer = buffer.subarray(size);
      waiter.resolve(out);
    }
  };
  socket.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    pump();
  });
  const finish = () => {
    ended = true;
    pump();
  };
  socket.on("end", finish);
  socket.on("close", finish);
  const take = (size) => new Promise((resolve, reject) => {
    waiters.push({ size, resolve, reject });
    pump();
  });
  return {
    exactly: (count) => take((data) => (data.length >= count ? count : null)),
    until: (marker) => take((data) => {
      const index = data.indexOf(marker);
      return index < 0 ? null : index + marker.length;
    }),
    ended: () => (ended ? Promise.resolve() : once(socket, "end")),
  };
}

async function readFrame(reader) {
  const [first, second] = await reader.exactly(2);
  assert.equal(second & 0x80, 0, "server frames must not be masked");
  let length = second & 0x7f;
  if (length === 126) length = (await reader.exactly(2)).readUInt16BE(0);
  else if (length === 127) length = Number((await reader.exactly(8)).readBigUInt64BE(0));
  const payload = length ? Buffer.from(await reader.exactly(length)) : Buffer.alloc(0);
  return {
    fin: (first & 0x80) !== 0,
    rsv: first & 0x70,
    opcode: first & 0x0f,
    lengthField: second & 0x7f,
    payload,
  };
}

function clientFrame(opcode, payload = Buffer.alloc(0), { fin = true, mask = true, rsv = 0 } = {}) {
  const data = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  let header;
  if (data.length < 126) {
    header = Buffer.from([0, data.length]);
  } else if (data.length <= 0xffff) {
    header = Buffer.alloc(4);
    header[1] = 126;
    header.writeUInt16BE(data.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(data.length), 2);
  }
  header[0] = (fin ? 0x80 : 0) | rsv | opcode;
  if (!mask) return Buffer.concat([header, data]);
  header[1] |= 0x80;
  const key = Buffer.from([0x37, 0xfa, 0x21, 0x3d]);
  return Buffer.concat([header, key, data.map((byte, index) => byte ^ key[index & 3])]);
}

function closePayload(code, reason = "") {
  const payload = Buffer.alloc(2);
  payload.writeUInt16BE(code, 0);
  return Buffer.concat([payload, Buffer.from(reason, "utf8")]);
}

function upgradeRequest(port, { key = SAMPLE_KEY, headers = [] } = {}) {
  return [
    "GET /ws HTTP/1.1",
    `Host: 127.0.0.1:${port}`,
    "Upgrade: websocket",
    "Connection: Upgrade",
    `Sec-WebSocket-Key: ${key}`,
    "Sec-WebSocket-Version: 13",
    ...headers,
    "",
    "",
  ].join("\r\n");
}

async function rawConnect(t, port) {
  const socket = connect(port, "127.0.0.1");
  t.after(() => socket.destroy());
  await once(socket, "connect");
  return { socket, reader: byteReader(socket) };
}

async function rawOpen(t, port, { key, headers, trailing } = {}) {
  const { socket, reader } = await rawConnect(t, port);
  const request = upgradeRequest(port, { key, headers });
  socket.write(trailing ? Buffer.concat([Buffer.from(request), trailing]) : request);
  const response = (await reader.until("\r\n\r\n")).toString("latin1");
  return { socket, reader, response };
}

async function nodeClient(t, port) {
  const client = new WebSocket(`ws://127.0.0.1:${port}/ws`);
  client.binaryType = "arraybuffer";
  t.after(() => client.close());
  await once(client, "open");
  return client;
}

function nextMessage(client) {
  return new Promise((resolve) => {
    client.addEventListener("message", (event) => resolve(event.data), { once: true });
  });
}

function fakeSocket({ hold = false } = {}) {
  const chunks = [];
  const pending = [];
  let holding = hold;
  const socket = new Duplex({
    read() {},
    write(chunk, encoding, callback) {
      chunks.push(Buffer.from(chunk));
      if (holding) pending.push(callback);
      else callback();
    },
  });
  return {
    socket,
    output: () => Buffer.concat(chunks),
    hold() {
      holding = true;
    },
    release() {
      holding = false;
      while (pending.length) pending.shift()();
    },
  };
}

/** Split what the server wrote after its 101 response into frames. */
function serverFrames(output) {
  const frames = [];
  let offset = output.indexOf("\r\n\r\n") + 4;
  while (offset < output.length) {
    const first = output[offset];
    let length = output[offset + 1] & 0x7f;
    let start = offset + 2;
    if (length === 126) {
      length = output.readUInt16BE(start);
      start += 2;
    } else if (length === 127) {
      length = Number(output.readBigUInt64BE(start));
      start += 8;
    }
    frames.push({ fin: (first & 0x80) !== 0, opcode: first & 0x0f, payload: output.subarray(start, start + length) });
    offset = start + length;
  }
  return frames;
}

// Small seeded generator (mulberry32) so random splits are reproducible.
function seededRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

function fakeRequest(headers = {}) {
  return {
    method: "GET",
    headers: {
      upgrade: "websocket",
      connection: "keep-alive, Upgrade",
      "sec-websocket-version": "13",
      "sec-websocket-key": SAMPLE_KEY,
      ...headers,
    },
  };
}

async function expectClose(reader, code) {
  const frame = await readFrame(reader);
  assert.equal(frame.opcode, OP.CLOSE);
  assert.equal(frame.fin, true);
  assert.equal(frame.payload.readUInt16BE(0), code);
  return frame;
}

test("websocket: handshake answers the RFC 6455 sample key and picks an offered subprotocol", TIMEOUT, async (t) => {
  assert.equal(websocketAcceptKey(SAMPLE_KEY), SAMPLE_ACCEPT);
  const server = await startServer(t, { options: { protocols: ["autonom.v1"] } });

  const chosen = await rawOpen(t, server.port, { headers: ["Sec-WebSocket-Protocol: chat, autonom.v1"] });
  assert.match(chosen.response, /^HTTP\/1\.1 101 Switching Protocols\r\n/);
  assert.match(chosen.response, /\r\nUpgrade: websocket\r\n/);
  assert.match(chosen.response, /\r\nConnection: Upgrade\r\n/);
  assert.ok(chosen.response.includes(`\r\nSec-WebSocket-Accept: ${SAMPLE_ACCEPT}\r\n`));
  assert.ok(chosen.response.includes("\r\nSec-WebSocket-Protocol: autonom.v1\r\n"));
  const first = await server.connectionAt(0);
  assert.equal(first.ws.protocol, "autonom.v1");
  assert.equal(first.ws.readyState, READY_STATE.OPEN);

  const none = await rawOpen(t, server.port, { headers: ["Sec-WebSocket-Protocol: chat"] });
  assert.match(none.response, /^HTTP\/1\.1 101 /);
  assert.equal(none.response.includes("Sec-WebSocket-Protocol"), false);
  assert.equal((await server.connectionAt(1)).ws.protocol, null);
});

test("websocket: invalid upgrade requests are answered with 400 or 426 and no connection", TIMEOUT, () => {
  const cases = [
    { request: { ...fakeRequest(), method: "POST" }, status: "400 Bad Request" },
    { request: fakeRequest({ upgrade: "h2c" }), status: "400 Bad Request" },
    { request: fakeRequest({ connection: "keep-alive" }), status: "400 Bad Request" },
    { request: fakeRequest({ "sec-websocket-key": undefined }), status: "400 Bad Request" },
    { request: fakeRequest({ "sec-websocket-key": "c2hvcnQ=" }), status: "400 Bad Request" },
    { request: fakeRequest({ "sec-websocket-version": "8" }), status: "426 Upgrade Required" },
  ];
  for (const { request, status } of cases) {
    const fake = fakeSocket();
    assert.equal(acceptWebSocket(request, fake.socket, Buffer.alloc(0)), null);
    const text = fake.output().toString("utf8");
    assert.ok(text.startsWith(`HTTP/1.1 ${status}\r\n`), text);
    assert.equal(text.includes("101 Switching"), false);
    if (status.startsWith("426")) assert.ok(text.includes("\r\nSec-WebSocket-Version: 13\r\n"));
    fake.socket.destroy();
  }

  const refused = fakeSocket();
  rejectUpgrade(refused.socket, 403, "Origin is not allowed.");
  const text = refused.output().toString("utf8");
  assert.ok(text.startsWith("HTTP/1.1 403 Forbidden\r\n"));
  assert.ok(text.endsWith("\r\n\r\nOrigin is not allowed.\n"));
  refused.socket.destroy();
});

test("websocket: text and binary messages round-trip with the Node WebSocket client", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const client = await nodeClient(t, server.port);

  let reply = nextMessage(client);
  client.send("Café ✓ naïve");
  assert.equal(await reply, "Café ✓ naïve");

  reply = nextMessage(client);
  client.send(Uint8Array.from([0, 1, 2, 255]));
  const binary = await reply;
  assert.ok(binary instanceof ArrayBuffer);
  assert.deepEqual([...new Uint8Array(binary)], [0, 1, 2, 255]);

  reply = nextMessage(client);
  client.send("");
  assert.equal(await reply, "");

  const record = await server.connectionAt(0);
  assert.deepEqual(record.messages.map((message) => message.isBinary), [false, true, false]);
  assert.equal(record.messages[0].data, "Café ✓ naïve");
  assert.ok(Buffer.isBuffer(record.messages[1].data));
});

test("websocket: 7-bit, 16-bit and 64-bit lengths parse, and server frames are unmasked and unfragmented", TIMEOUT, async (t) => {
  const server = await startServer(t, { options: { maxMessageBytes: 256 * 1024 } });
  const { socket, reader } = await rawOpen(t, server.port);

  socket.write(clientFrame(OP.TEXT, Buffer.from("hi")));
  let frame = await readFrame(reader);
  assert.deepEqual(
    { fin: frame.fin, rsv: frame.rsv, opcode: frame.opcode, lengthField: frame.lengthField },
    { fin: true, rsv: 0, opcode: OP.TEXT, lengthField: 2 },
  );
  assert.equal(frame.payload.toString("utf8"), "hi");

  const medium = Buffer.alloc(300, 7);
  socket.write(clientFrame(OP.BINARY, medium));
  frame = await readFrame(reader);
  assert.equal(frame.fin, true);
  assert.equal(frame.opcode, OP.BINARY);
  assert.equal(frame.lengthField, 126);
  assert.deepEqual(frame.payload, medium);

  const large = Buffer.alloc(70 * 1024);
  for (let i = 0; i < large.length; i += 1) large[i] = (i * 31) & 0xff;
  socket.write(clientFrame(OP.BINARY, large));
  frame = await readFrame(reader);
  assert.equal(frame.fin, true);
  assert.equal(frame.opcode, OP.BINARY);
  assert.equal(frame.lengthField, 127);
  assert.deepEqual(frame.payload, large);

  const record = await server.connectionAt(0);
  assert.equal(record.messages.length, 3);
  assert.deepEqual(record.messages[2].data, large);
});

test("websocket: 64-bit length messages round-trip with the Node client under a raised limit", TIMEOUT, async (t) => {
  const server = await startServer(t, { options: { maxMessageBytes: 1024 * 1024 } });
  const client = await nodeClient(t, server.port);
  const payload = new Uint8Array(70 * 1024).map((_, index) => index % 251);
  const reply = nextMessage(client);
  client.send(payload);
  assert.deepEqual(new Uint8Array(await reply), payload);
});

test("websocket: oversize message from the Node client closes with 1009 at the 64 KiB default", TIMEOUT, async (t) => {
  assert.equal(DEFAULT_MAX_MESSAGE_BYTES, 64 * 1024);
  const server = await startServer(t);
  const client = await nodeClient(t, server.port);

  const reply = nextMessage(client);
  client.send(new Uint8Array(DEFAULT_MAX_MESSAGE_BYTES));
  assert.equal((await reply).byteLength, DEFAULT_MAX_MESSAGE_BYTES);

  const closed = once(client, "close");
  client.send(new Uint8Array(70 * 1024));
  const [event] = await closed;
  assert.equal(event.code, CLOSE_CODE.MESSAGE_TOO_BIG);

  const record = await server.connectionAt(0);
  assert.deepEqual(await record.closed, { code: 1009, reason: "Message is too big." });
  assert.equal(record.messages.length, 1);
});

test("websocket: an oversize length in the frame header is refused before its payload arrives", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const { socket, reader } = await rawOpen(t, server.port);
  const header = Buffer.from([0x82, 0xff, 0, 0, 0, 0, 0, 0x01, 0x18, 0x00, 1, 2, 3, 4]);
  socket.write(header);
  await expectClose(reader, CLOSE_CODE.MESSAGE_TOO_BIG);
  await reader.ended();
  const record = await server.connectionAt(0);
  assert.equal((await record.closed).code, 1009);
});

test("websocket: fragmented messages with an interleaved ping are reassembled and the pong echoes the payload", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const { socket, reader } = await rawOpen(t, server.port);
  // The two-byte "ö" is split across fragments; UTF-8 is checked on the whole message.
  const text = Buffer.from("Hello wörld", "utf8");
  socket.write(clientFrame(OP.TEXT, text.subarray(0, 3), { fin: false }));
  socket.write(clientFrame(OP.PING, Buffer.from("p1")));
  socket.write(clientFrame(OP.CONTINUATION, text.subarray(3, 8), { fin: false }));
  socket.write(clientFrame(OP.CONTINUATION, text.subarray(8)));

  const pong = await readFrame(reader);
  assert.equal(pong.opcode, OP.PONG);
  assert.equal(pong.fin, true);
  assert.equal(pong.payload.toString("utf8"), "p1");
  const reply = await readFrame(reader);
  assert.equal(reply.opcode, OP.TEXT);
  assert.equal(reply.payload.toString("utf8"), "Hello wörld");

  socket.write(clientFrame(OP.BINARY, Buffer.from([1, 2]), { fin: false }));
  socket.write(clientFrame(OP.CONTINUATION, Buffer.from([3]), { fin: false }));
  socket.write(clientFrame(OP.CONTINUATION, Buffer.from([4, 5])));
  const binary = await readFrame(reader);
  assert.equal(binary.opcode, OP.BINARY);
  assert.deepEqual([...binary.payload], [1, 2, 3, 4, 5]);

  const record = await server.connectionAt(0);
  assert.deepEqual(
    record.messages.map(({ data, isBinary }) => [isBinary ? [...data] : data, isBinary]),
    [["Hello wörld", false], [[1, 2, 3, 4, 5], true]],
  );
});

test("websocket: fragments that together exceed the limit close with 1009", TIMEOUT, async (t) => {
  const server = await startServer(t, { options: { maxMessageBytes: 10 } });
  const { socket, reader } = await rawOpen(t, server.port);
  socket.write(clientFrame(OP.TEXT, Buffer.from("123456"), { fin: false }));
  socket.write(clientFrame(OP.CONTINUATION, Buffer.from("789012")));
  await expectClose(reader, CLOSE_CODE.MESSAGE_TOO_BIG);
  const record = await server.connectionAt(0);
  assert.equal((await record.closed).code, 1009);
  assert.equal(record.messages.length, 0);
});

test("websocket: an unmasked client frame closes with 1002", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const { socket, reader } = await rawOpen(t, server.port);
  socket.write(clientFrame(OP.TEXT, Buffer.from("hi"), { mask: false }));
  await expectClose(reader, CLOSE_CODE.PROTOCOL_ERROR);
  await reader.ended();
  const record = await server.connectionAt(0);
  assert.deepEqual(await record.closed, { code: 1002, reason: "Client frames must be masked." });
  assert.equal(record.messages.length, 0);
});

test("websocket: protocol violations close with 1002", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const topBitLength = Buffer.from([0x82, 0xff, 0x80, 0, 0, 0, 0, 0, 0, 0x01, 1, 2, 3, 4]);
  const cases = [
    ["reserved bit", [clientFrame(OP.TEXT, "x", { rsv: 0x40 })]],
    ["unknown data opcode", [clientFrame(0x3, "x")]],
    ["unknown control opcode", [clientFrame(0xb, "x")]],
    ["continuation without a message", [clientFrame(OP.CONTINUATION, "x")]],
    ["new message inside a fragmented one", [clientFrame(OP.TEXT, "a", { fin: false }), clientFrame(OP.TEXT, "b")]],
    ["ping longer than 125 bytes", [clientFrame(OP.PING, Buffer.alloc(126))]],
    ["fragmented ping", [clientFrame(OP.PING, "x", { fin: false })]],
    ["64-bit length with the top bit set", [topBitLength]],
    ["one-byte close payload", [clientFrame(OP.CLOSE, Buffer.from([0x03]))]],
    ["close code 1005 on the wire", [clientFrame(OP.CLOSE, closePayload(1005))]],
  ];
  let index = 0;
  for (const [name, frames] of cases) {
    await t.test(`websocket protocol violation: ${name}`, async (st) => {
      const { socket, reader } = await rawOpen(st, server.port);
      for (const frame of frames) socket.write(frame);
      await expectClose(reader, CLOSE_CODE.PROTOCOL_ERROR);
      const record = await server.connectionAt(index);
      assert.equal((await record.closed).code, 1002);
      assert.equal(record.messages.length, 0);
    });
    index += 1;
  }
});

test("websocket: invalid UTF-8 in text or close reason closes with 1007", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const text = await rawOpen(t, server.port);
  text.socket.write(clientFrame(OP.TEXT, Buffer.from([0x48, 0xff, 0xfe])));
  await expectClose(text.reader, CLOSE_CODE.INVALID_PAYLOAD);
  const first = await server.connectionAt(0);
  assert.equal((await first.closed).code, 1007);
  assert.equal(first.messages.length, 0);

  const reason = await rawOpen(t, server.port);
  reason.socket.write(clientFrame(OP.CLOSE, Buffer.concat([closePayload(1000), Buffer.from([0xc3])])));
  await expectClose(reason.reader, CLOSE_CODE.INVALID_PAYLOAD);
  assert.equal((await (await server.connectionAt(1)).closed).code, 1007);
});

test("websocket: client close handshake is echoed, then the server ends TCP first", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const coded = await rawOpen(t, server.port);
  coded.socket.write(clientFrame(OP.CLOSE, closePayload(1000, "bye")));
  const frame = await expectClose(coded.reader, 1000);
  assert.equal(frame.payload.length, 2);
  await coded.reader.ended();
  const first = await server.connectionAt(0);
  assert.deepEqual(await first.closed, { code: 1000, reason: "bye" });

  const empty = await rawOpen(t, server.port);
  empty.socket.write(clientFrame(OP.CLOSE));
  const reply = await readFrame(empty.reader);
  assert.equal(reply.opcode, OP.CLOSE);
  assert.equal(reply.payload.length, 0);
  assert.deepEqual(await (await server.connectionAt(1)).closed, { code: CLOSE_CODE.NO_STATUS, reason: "" });
});

test("websocket: Node client close reaches the server with code and reason", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const client = await nodeClient(t, server.port);
  const closed = once(client, "close");
  client.close(1000, "finished");
  const [event] = await closed;
  assert.equal(event.code, 1000);
  assert.equal(event.wasClean, true);
  assert.deepEqual(await (await server.connectionAt(0)).closed, { code: 1000, reason: "finished" });
});

test("websocket: server close reaches the Node client with code and reason", TIMEOUT, async (t) => {
  const server = await startServer(t, {
    onConnection(ws) {
      ws.close(4000, "done");
      assert.equal(ws.readyState, READY_STATE.CLOSING);
      assert.equal(ws.send("too late"), false);
      ws.close(1000);
    },
  });
  const client = new WebSocket(`ws://127.0.0.1:${server.port}/ws`);
  const messages = [];
  client.addEventListener("message", (event) => messages.push(event.data));
  const [event] = await once(client, "close");
  assert.equal(event.code, 4000);
  assert.equal(event.reason, "done");
  assert.equal(event.wasClean, true);
  assert.deepEqual(messages, []);
  const record = await server.connectionAt(0);
  assert.deepEqual(await record.closed, { code: 4000, reason: "done" });
  assert.equal(record.ws.readyState, READY_STATE.CLOSED);
});

test("websocket: an abrupt disconnect is reported as 1006", TIMEOUT, async (t) => {
  const server = await startServer(t);
  const { socket } = await rawOpen(t, server.port);
  const record = await server.connectionAt(0);
  socket.destroy();
  assert.deepEqual(await record.closed, { code: CLOSE_CODE.ABNORMAL, reason: "" });
  assert.equal(record.ws.send("gone"), false);
});

test("websocket: a socket that closed before the upgrade was accepted reports one 1006 close", TIMEOUT, async () => {
  const fake = fakeSocket();
  fake.socket.destroy();
  await once(fake.socket, "close");
  const head = clientFrame(OP.TEXT, "late");
  const ws = acceptWebSocket(fakeRequest(), fake.socket, head);
  assert.notEqual(ws, null);
  const closes = [];
  const messages = [];
  ws.on("close", (code, reason) => closes.push({ code, reason }));
  ws.on("message", (data) => messages.push(data));
  await tick();
  await tick();
  assert.deepEqual(closes, [{ code: CLOSE_CODE.ABNORMAL, reason: "" }]);
  assert.deepEqual(messages, []);
  assert.equal(ws.readyState, READY_STATE.CLOSED);
  assert.equal(ws.send("gone"), false);
  assert.equal(fake.output().length, 0);
});

test("websocket: a peer that sent FIN while a delayed accept waited is reported as one 1006 close", TIMEOUT, async (t) => {
  const atAccept = [];
  const server = await startServer(t, {
    async beforeAccept(request, socket) {
      // Authorization finishes only after the client has gone.
      if (!socket.readableEnded) await once(socket, "end");
      atAccept.push({ readableEnded: socket.readableEnded, destroyed: socket.destroyed });
    },
  });
  const cases = [
    ["half close", (socket) => socket.end()],
    ["full close", (socket) => socket.destroy()],
  ];
  for (const [index, [name, leave]] of cases.entries()) {
    const { socket, reader } = await rawConnect(t, server.port);
    socket.write(upgradeRequest(server.port), () => leave(socket));
    const record = await server.connectionAt(index);
    assert.deepEqual(await record.closed, { code: CLOSE_CODE.ABNORMAL, reason: "" }, name);
    assert.equal(record.ws.readyState, READY_STATE.CLOSED, name);
    assert.equal(record.ws.send("gone"), false, name);
    if (name === "half close") {
      // A half-closed peer still reads: it gets the 101 and then the server's FIN.
      assert.match((await reader.until("\r\n\r\n")).toString("latin1"), /^HTTP\/1\.1 101 /);
      await reader.ended();
    }
  }
  assert.deepEqual(atAccept, [
    { readableEnded: true, destroyed: false },
    { readableEnded: true, destroyed: false },
  ]);
});

test("websocket: with a delayed accept, frames sent with the upgrade request are parsed before bytes that arrived later", TIMEOUT, async (t) => {
  const a = clientFrame(OP.TEXT, "A");
  const b = clientFrame(OP.TEXT, "B");
  let upgraded = null;
  let laterBytes = 0;
  const server = await startServer(t, {
    async beforeAccept(request, socket) {
      upgraded();
      // Hold the accept until the later bytes wait in the socket's own read buffer.
      while (!socket.destroyed && socket.readableLength < laterBytes) await delay(5);
    },
  });
  const cases = [
    ["whole frame in the head", a, b],
    ["frame split across the head", a.subarray(0, 3), Buffer.concat([a.subarray(3), b])],
  ];
  for (const [index, [name, early, later]] of cases.entries()) {
    const seen = new Promise((resolve) => { upgraded = resolve; });
    laterBytes = later.length;
    const { socket, reader } = await rawConnect(t, server.port);
    socket.write(Buffer.concat([Buffer.from(upgradeRequest(server.port)), early]));
    await seen;
    socket.write(later);
    assert.match((await reader.until("\r\n\r\n")).toString("latin1"), /^HTTP\/1\.1 101 /, name);
    const replies = [await readFrame(reader), await readFrame(reader)];
    assert.deepEqual(
      replies.map((frame) => [frame.opcode, frame.payload.toString("utf8")]),
      [[OP.TEXT, "A"], [OP.TEXT, "B"]],
      name,
    );
    const record = await server.connectionAt(index);
    assert.deepEqual(record.messages.map(({ data }) => data), ["A", "B"], name);
    assert.equal(record.ws.readyState, READY_STATE.OPEN, name);
  }
});

test("websocket: early bytes are parsed before bytes already waiting in the socket buffer, and a FIN behind them closes with 1006", TIMEOUT, async (t) => {
  const a = clientFrame(OP.TEXT, "A");
  const b = clientFrame(OP.BINARY, Buffer.from([2]));
  for (const split of [a.length, 3, 1]) {
    for (const fin of [false, true]) {
      const label = `split ${split}, fin ${fin}`;
      const fake = fakeSocket();
      t.after(() => fake.socket.destroy());
      // Bytes that reach a socket nobody reads yet stay in its readable buffer.
      fake.socket.push(Buffer.concat([a.subarray(split), b]));
      if (fin) fake.socket.push(null);
      const ws = acceptWebSocket(fakeRequest(), fake.socket, a.subarray(0, split));
      const received = [];
      const closes = [];
      const closed = once(ws, "close");
      ws.on("message", (data, isBinary) => received.push([isBinary ? [...data] : data, isBinary]));
      ws.on("close", (code) => closes.push(code));
      if (fin) await closed;
      else await tick();
      assert.deepEqual(received, [["A", false], [[2], true]], label);
      assert.deepEqual(closes, fin ? [CLOSE_CODE.ABNORMAL] : [], label);
      assert.equal(ws.readyState, fin ? READY_STATE.CLOSED : READY_STATE.OPEN, label);
    }
  }
});

test("websocket: a peer that ended before the accept still gets its early frames parsed, then one 1006 close", TIMEOUT, async (t) => {
  const fake = fakeSocket();
  t.after(() => fake.socket.destroy());
  // As on a real socket, a FIN with nothing buffered emits "end" even though nobody reads.
  fake.socket.push(null);
  fake.socket.read(0);
  await once(fake.socket, "end");
  assert.equal(fake.socket.destroyed, false);

  const ws = acceptWebSocket(fakeRequest(), fake.socket, clientFrame(OP.TEXT, "last"));
  const received = [];
  const closes = [];
  ws.on("message", (data) => {
    received.push(data);
    ws.send(`echo ${data}`);
  });
  ws.on("close", (code, reason) => closes.push({ code, reason }));
  await once(ws, "close");
  await tick();
  assert.deepEqual(received, ["last"]);
  assert.deepEqual(closes, [{ code: CLOSE_CODE.ABNORMAL, reason: "" }]);
  assert.equal(ws.readyState, READY_STATE.CLOSED);
  assert.equal(fake.socket.writableEnded, true);
  // The reply written from the message handler goes out before the server's FIN.
  assert.deepEqual(
    serverFrames(fake.output()).map((frame) => [frame.opcode, frame.payload.toString("utf8")]),
    [[OP.TEXT, "echo last"]],
  );
});

test("websocket: frames sent together with the upgrade request are delivered", TIMEOUT, async (t) => {
  const fake = fakeSocket();
  t.after(() => fake.socket.destroy());
  const head = Buffer.concat([clientFrame(OP.TEXT, "early"), clientFrame(OP.BINARY, Buffer.from([9]))]);
  const ws = acceptWebSocket(fakeRequest(), fake.socket, head);
  const received = [];
  ws.on("message", (data, isBinary) => received.push([isBinary ? [...data] : data, isBinary]));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(received, [["early", false], [[9], true]]);

  const server = await startServer(t);
  const { reader } = await rawOpen(t, server.port, { trailing: clientFrame(OP.TEXT, "with-upgrade") });
  const reply = await readFrame(reader);
  assert.equal(reply.payload.toString("utf8"), "with-upgrade");
});

test("websocket: bufferedAmount mirrors socket.writableLength and close reasons are bounded", TIMEOUT, async (t) => {
  const fake = fakeSocket({ hold: true });
  t.after(() => fake.socket.destroy());
  const ws = acceptWebSocket(fakeRequest(), fake.socket, Buffer.alloc(0));
  const handshakeBytes = fake.socket.writableLength;
  assert.ok(handshakeBytes > 0);
  assert.equal(ws.bufferedAmount, handshakeBytes);

  assert.equal(typeof ws.send(Buffer.alloc(1000, 1)), "boolean");
  assert.equal(ws.bufferedAmount, fake.socket.writableLength);
  assert.equal(ws.bufferedAmount, handshakeBytes + 4 + 1000);

  fake.release();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ws.bufferedAmount, 0);
  const output = fake.output();
  const frameStart = output.indexOf("\r\n\r\n") + 4;
  assert.deepEqual([...output.subarray(frameStart, frameStart + 4)], [0x82, 126, 0x03, 0xe8]);

  assert.throws(() => ws.close(1005), RangeError);
  ws.close(4001, "é".repeat(70));
  const closeFrame = fake.output().subarray(frameStart + 4 + 1000);
  assert.equal(closeFrame[0], 0x88);
  assert.equal(closeFrame[1], 2 + 122);
  assert.equal(closeFrame.readUInt16BE(2), 4001);
  assert.equal(closeFrame.subarray(4).toString("utf8"), "é".repeat(61));
  assert.equal(ws.send("after close"), false);
  ws.close(1000);
  assert.equal(fake.output().length, frameStart + 4 + 1000 + 2 + 2 + 122);
});

test("websocket: a flood of empty continuation frames keeps memory bounded and the message still completes", TIMEOUT, async (t) => {
  setFlagsFromString("--expose-gc");
  const gc = runInNewContext("gc");
  const fake = fakeSocket();
  t.after(() => fake.socket.destroy());
  const ws = acceptWebSocket(fakeRequest(), fake.socket, Buffer.alloc(0));
  const received = [];
  ws.on("message", (data) => received.push(data));

  fake.socket.emit("data", clientFrame(OP.TEXT, "a", { fin: false }));
  const empty = clientFrame(OP.CONTINUATION, Buffer.alloc(0), { fin: false });
  const batch = Buffer.concat(Array.from({ length: 10_000 }, () => empty));
  gc();
  const before = process.memoryUsage().heapUsed;
  // 1,000,000 empty fragments, 6 MB on the wire; one stored Buffer each used to cost about 200 MB.
  for (let i = 0; i < 100; i += 1) fake.socket.emit("data", batch);
  gc();
  const growth = process.memoryUsage().heapUsed - before;
  assert.ok(growth < 16 * 1024 * 1024, `heap grew by ${growth} bytes`);
  assert.equal(ws.readyState, READY_STATE.OPEN);

  fake.socket.emit("data", clientFrame(OP.CONTINUATION, "b"));
  assert.deepEqual(received, ["ab"]);
});

test("websocket: one-byte and empty fragments assemble a message at the 64 KiB limit and one byte more closes with 1009", TIMEOUT, async (t) => {
  const fake = fakeSocket();
  t.after(() => fake.socket.destroy());
  const ws = acceptWebSocket(fakeRequest(), fake.socket, Buffer.alloc(0));
  const received = [];
  ws.on("message", (data, isBinary) => received.push({ data, isBinary }));

  const expected = Buffer.alloc(DEFAULT_MAX_MESSAGE_BYTES);
  for (let i = 0; i < expected.length; i += 1) expected[i] = (i * 7) & 0xff;
  const frames = [clientFrame(OP.BINARY, expected.subarray(0, 1), { fin: false })];
  for (let i = 1; i < expected.length; i += 1) {
    frames.push(clientFrame(OP.CONTINUATION, Buffer.alloc(0), { fin: false }));
    frames.push(clientFrame(OP.CONTINUATION, expected.subarray(i, i + 1), { fin: i === expected.length - 1 }));
  }
  fake.socket.emit("data", Buffer.concat(frames));
  assert.equal(received.length, 1);
  assert.equal(received[0].isBinary, true);
  assert.deepEqual(received[0].data, expected);

  const over = [clientFrame(OP.TEXT, "x", { fin: false })];
  for (let i = 1; i <= DEFAULT_MAX_MESSAGE_BYTES; i += 1) {
    over.push(clientFrame(OP.CONTINUATION, "x", { fin: i === DEFAULT_MAX_MESSAGE_BYTES }));
  }
  fake.socket.emit("data", Buffer.concat(over));
  const close = serverFrames(fake.output()).at(-1);
  assert.equal(close.opcode, OP.CLOSE);
  assert.equal(close.payload.readUInt16BE(0), CLOSE_CODE.MESSAGE_TOO_BIG);
  assert.equal(ws.readyState, READY_STATE.CLOSING);
  assert.equal(received.length, 1);
});

test("websocket: pings from a peer that never reads leave at most one pending pong, sent on drain or with the next message", TIMEOUT, async (t) => {
  const fake = fakeSocket({ hold: true });
  t.after(() => fake.socket.destroy());
  const ws = acceptWebSocket(fakeRequest(), fake.socket, Buffer.alloc(0));
  const label = (n) => String(n).padStart(125, "0");
  const pings = (from, count) => Buffer.concat(
    Array.from({ length: count }, (_, i) => clientFrame(OP.PING, Buffer.from(label(from + i)))),
  );
  // Writing stops being eager once the socket needs a drain, so at most one more pong fits.
  const bound = fake.socket.writableHighWaterMark + 2 + 125;
  const count = 10_000;

  fake.socket.emit("data", pings(0, count));
  assert.ok(ws.bufferedAmount <= bound, `bufferedAmount ${ws.bufferedAmount} > ${bound}`);
  assert.equal(ws.readyState, READY_STATE.OPEN);
  fake.release();
  await tick();
  assert.equal(ws.bufferedAmount, 0);
  const pongs = serverFrames(fake.output()).filter((frame) => frame.opcode === OP.PONG);
  assert.ok(pongs.length < count / 10, `${pongs.length} pongs for ${count} pings`);
  assert.equal(pongs.at(-1).payload.toString("latin1"), label(count - 1));

  fake.hold();
  fake.socket.emit("data", pings(count, count));
  assert.ok(ws.bufferedAmount <= bound, `bufferedAmount ${ws.bufferedAmount} > ${bound}`);
  ws.send("after");
  fake.release();
  await tick();
  const tail = serverFrames(fake.output()).slice(-2);
  assert.deepEqual(tail.map((frame) => frame.opcode), [OP.PONG, OP.TEXT]);
  assert.equal(tail[0].payload.toString("latin1"), label(2 * count - 1));
  assert.equal(tail[1].payload.toString("utf8"), "after");
});

test("websocket: frames split at any byte boundary, including inside the upgrade head, parse like whole frames", TIMEOUT, async (t) => {
  const text = Buffer.from("Hello wörld", "utf8");
  const big = Buffer.alloc(70 * 1024);
  for (let i = 0; i < big.length; i += 1) big[i] = (i * 13) & 0xff;
  const stream = Buffer.concat([
    clientFrame(OP.TEXT, "first"),
    clientFrame(OP.BINARY, Buffer.alloc(300, 7)),
    clientFrame(OP.TEXT, text.subarray(0, 3), { fin: false }),
    clientFrame(OP.PING, "p1"),
    clientFrame(OP.CONTINUATION, Buffer.alloc(0), { fin: false }),
    clientFrame(OP.CONTINUATION, text.subarray(3)),
    clientFrame(OP.BINARY, big),
    clientFrame(OP.TEXT, ""),
  ]);
  const expected = [
    ["first", false],
    [Buffer.alloc(300, 7), true],
    ["Hello wörld", false],
    [big, true],
    ["", false],
  ];

  async function deliver(headBytes, nextSize) {
    const fake = fakeSocket();
    t.after(() => fake.socket.destroy());
    const ws = acceptWebSocket(fakeRequest(), fake.socket, stream.subarray(0, headBytes), { maxMessageBytes: 128 * 1024 });
    const received = [];
    ws.on("message", (data, isBinary) => received.push([data, isBinary]));
    await tick();
    for (let offset = headBytes; offset < stream.length;) {
      const size = nextSize();
      fake.socket.emit("data", stream.subarray(offset, offset + size));
      offset += size;
    }
    assert.deepEqual(received, expected);
    const pongs = serverFrames(fake.output()).filter((frame) => frame.opcode === OP.PONG);
    assert.deepEqual(pongs.map((frame) => frame.payload.toString("utf8")), ["p1"]);
    assert.equal(ws.readyState, READY_STATE.OPEN);
  }

  await deliver(0, () => stream.length);
  await deliver(0, () => 1);
  for (const headBytes of [1, 3, 9, 20]) await deliver(headBytes, () => 1);
  for (let seed = 1; seed <= 25; seed += 1) {
    const random = seededRandom(seed);
    await deliver(Math.floor(random() * 12), () => 1 + Math.floor(random() * (random() < 0.5 ? 8 : 4096)));
  }
});
