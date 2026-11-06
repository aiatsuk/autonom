"""A minimal RFC 6455 WebSocket client for the server's own Canvas (contract 5.2).

The client speaks to ``127.0.0.1`` over a plain socket as an API client: the
handshake sends a random ``Sec-WebSocket-Key``, checks ``Sec-WebSocket-Accept``,
carries ``Authorization: Bearer`` and never an ``Origin`` header (the Canvas
treats a request with an Origin as a browser page). Client frames are masked;
text, binary and continuation frames are reassembled into messages of at most
8 MiB; a ping is answered with a pong; a close frame is answered and ends the
connection. Reads time out after 30 s (``WebSocketTimeout``; the connection
stays usable).

The Canvas video messages (``VIDEO_MESSAGE`` and ``encodeVideo*`` in the Canvas
code) are decoded by ``parse_video_message``: ``1`` session (width, height,
u32 big endian), ``2`` config (codec bytes), ``3`` packet (key u8, pts u64 big
endian, data). ``codec_from_sps`` derives the ``avc1.PPCCLL`` string from the
SPS in a config, exactly as the Canvas page does.

Python 3.9 compatible, standard library only. Nothing here ever logs a header
value, so the Bearer token cannot leak through this module.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
READ_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 10.0
MAX_HANDSHAKE_BYTES = 16 * 1024
RECV_CHUNK = 256 * 1024

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
DATA_OPCODES = (OP_CONTINUATION, OP_TEXT, OP_BINARY)
CONTROL_OPCODES = (OP_CLOSE, OP_PING, OP_PONG)

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_INVALID_PAYLOAD = 1007
CLOSE_MESSAGE_TOO_BIG = 1009

VIDEO_SESSION = 1
VIDEO_CONFIG = 2
VIDEO_PACKET = 3


class WebSocketError(Exception):
    """A failed handshake, a protocol violation or a lost connection."""


class WebSocketTimeout(WebSocketError):
    """No complete frame arrived within the read timeout; the connection is still usable."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (``code`` and ``reason`` of its close frame)."""

    def __init__(self, code: Optional[int], reason: str = "") -> None:
        super().__init__("the WebSocket was closed%s" % ("" if code is None else " (%d)" % code))
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value a server must answer for ``key``."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask_bytes(data: bytes, key: bytes) -> bytes:
    """``data`` XOR the repeated 4-byte ``key`` (RFC 6455 section 5.3)."""
    if not data:
        return b""
    size = len(data)
    pad = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(size, "big")


def encode_frame(opcode: int, payload: bytes, *, fin: bool = True, mask_key: Optional[bytes] = None) -> bytes:
    """One client frame: masked with ``mask_key`` (random when None)."""
    key = mask_key if mask_key is not None else os.urandom(4)
    first = (0x80 if fin else 0) | (opcode & 0x0F)
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", first, 0x80 | size)
    elif size < 65536:
        header = struct.pack("!BBH", first, 0x80 | 126, size)
    else:
        header = struct.pack("!BBQ", first, 0x80 | 127, size)
    return header + key + mask_bytes(payload, key)


def _header_value(value: str) -> str:
    text = str(value)
    if "\r" in text or "\n" in text or "\0" in text:
        raise WebSocketError("a handshake header holds a line break")
    return text


class WebSocketClient:
    """One client connection. ``recv`` is called from one thread; ``send_*`` from any."""

    def __init__(self, sock: socket.socket, *, buffered: bytes = b"",
                 max_message_bytes: int = MAX_MESSAGE_BYTES, read_timeout: float = READ_TIMEOUT_S) -> None:
        self.sock = sock
        self.max_message_bytes = max_message_bytes
        self.read_timeout = read_timeout
        self._buffer = bytearray(buffered)
        self._send_lock = threading.Lock()
        self._fragments: Optional[bytearray] = None
        self._fragment_opcode = 0
        self._close_sent = False
        self.closed = False
        self.close_code: Optional[int] = None
        self.close_reason = ""
        sock.settimeout(read_timeout)

    # -- connect

    @classmethod
    def connect(cls, host: str, port: int, path: str, headers: Optional[Mapping[str, str]] = None, *,
                timeout: float = CONNECT_TIMEOUT_S, read_timeout: float = READ_TIMEOUT_S,
                max_message_bytes: int = MAX_MESSAGE_BYTES) -> "WebSocketClient":
        """Open ``ws://host:port/path`` and finish the handshake (``WebSocketError`` otherwise)."""
        if not path.startswith("/"):
            raise WebSocketError("the request path must start with /")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = ["GET %s HTTP/1.1" % _header_value(path), "Host: %s:%d" % (host, int(port)),
                 "Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: " + key,
                 "Sec-WebSocket-Version: 13"]
        for name, value in (headers or {}).items():
            if name.lower() in ("origin", "host", "upgrade", "connection") or name.lower().startswith("sec-websocket"):
                raise WebSocketError("the handshake header %s is set by the client itself" % name)
            lines.append("%s: %s" % (_header_value(name), _header_value(value)))
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        try:
            sock = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as exc:
            raise WebSocketError("the WebSocket connection failed: %s" % type(exc).__name__)
        try:
            sock.settimeout(timeout)
            sock.sendall(request)
            head, rest = cls._read_handshake(sock)
            cls._check_handshake(head, key)
        except WebSocketError:
            _close_quietly(sock)
            raise
        except OSError as exc:
            _close_quietly(sock)
            raise WebSocketError("the WebSocket handshake failed: %s" % type(exc).__name__)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return cls(sock, buffered=rest, max_message_bytes=max_message_bytes, read_timeout=read_timeout)

    @staticmethod
    def _read_handshake(sock: socket.socket) -> Tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            if len(data) > MAX_HANDSHAKE_BYTES:
                raise WebSocketError("the handshake answer is too long")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                raise WebSocketError("the handshake answer did not arrive in time")
            if not chunk:
                raise WebSocketError("the server closed the connection during the handshake")
            data += chunk
        head, _sep, rest = data.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    def _check_handshake(head: bytes, key: str) -> None:
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or not status[0].startswith("HTTP/1.") or status[1] != "101":
            code = status[1] if len(status) > 1 and status[1].isdigit() else "?"
            raise WebSocketError("the server refused the WebSocket upgrade (HTTP %s)" % code)
        fields: Dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                fields[name.strip().lower()] = value.strip()
        if fields.get("upgrade", "").lower() != "websocket":
            raise WebSocketError("the upgrade answer does not name websocket")
        if "upgrade" not in [item.strip().lower() for item in fields.get("connection", "").split(",")]:
            raise WebSocketError("the upgrade answer does not name Connection: Upgrade")
        if fields.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("the upgrade answer has a wrong Sec-WebSocket-Accept")
        if fields.get("sec-websocket-extensions"):
            raise WebSocketError("the server chose an extension the client did not offer")

    # -- send

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = encode_frame(opcode, payload)
        with self._send_lock:
            if self._close_sent and opcode != OP_CLOSE:
                raise WebSocketError("the WebSocket is closing")
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                raise WebSocketError("the WebSocket send failed: %s" % type(exc).__name__)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, bytes(data))

    def ping(self, data: bytes = b"") -> None:
        if len(data) > 125:
            raise WebSocketError("a ping payload is at most 125 bytes")
        self._send_frame(OP_PING, bytes(data))

    def _send_close(self, code: Optional[int], reason: str = "") -> None:
        with self._send_lock:
            if self._close_sent:
                return
            self._close_sent = True
            payload = b"" if code is None else struct.pack("!H", code) + reason.encode("utf-8")[:123]
            try:
                self.sock.sendall(encode_frame(OP_CLOSE, payload))
            except OSError:
                pass

    def close(self, code: int = CLOSE_NORMAL, reason: str = "", wait_s: float = 0.0) -> None:
        """Send a close frame, optionally wait for the peer's, then drop the socket.

        Pass ``wait_s`` only when no other thread reads this connection."""
        self._send_close(code, reason)
        if wait_s > 0 and not self.closed:
            try:
                self.sock.settimeout(wait_s)
                while True:
                    self.recv()
            except WebSocketError:
                pass
            except OSError:
                pass
        self._drop()

    def _drop(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        _close_quietly(self.sock)

    def _fail(self, code: int, message: str) -> WebSocketError:
        self._send_close(code, "")
        self._drop()
        return WebSocketError(message)

    # -- receive

    def _fill(self, size: int) -> None:
        while len(self._buffer) < size:
            try:
                chunk = self.sock.recv(RECV_CHUNK)
            except socket.timeout:
                raise WebSocketTimeout("no WebSocket data within the read timeout")
            except OSError as exc:
                self.closed = True
                raise WebSocketError("the WebSocket read failed: %s" % type(exc).__name__)
            if not chunk:
                self.closed = True
                raise WebSocketClosed(None, "the connection ended without a close frame")
            self._buffer += chunk

    def _next_frame(self) -> Tuple[bool, int, bytes]:
        """One whole frame from the buffer (bytes stay buffered until it is complete)."""
        self._fill(2)
        first, second = self._buffer[0], self._buffer[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if first & 0x70:
            raise self._fail(CLOSE_PROTOCOL_ERROR, "the server set reserved WebSocket bits")
        if second & 0x80:
            raise self._fail(CLOSE_PROTOCOL_ERROR, "the server sent a masked frame")
        if opcode not in DATA_OPCODES and opcode not in CONTROL_OPCODES:
            raise self._fail(CLOSE_PROTOCOL_ERROR, "the server sent an unknown opcode")
        size = second & 0x7F
        offset = 2
        if size == 126:
            self._fill(4)
            size = struct.unpack("!H", bytes(self._buffer[2:4]))[0]
            offset = 4
        elif size == 127:
            self._fill(10)
            size = struct.unpack("!Q", bytes(self._buffer[2:10]))[0]
            offset = 10
        if opcode in CONTROL_OPCODES and (size > 125 or not fin):
            raise self._fail(CLOSE_PROTOCOL_ERROR, "the server sent a long or fragmented control frame")
        pending = len(self._fragments) if self._fragments is not None and opcode == OP_CONTINUATION else 0
        if size > self.max_message_bytes or pending + size > self.max_message_bytes:
            raise self._fail(CLOSE_MESSAGE_TOO_BIG, "a WebSocket message is larger than %d bytes"
                             % self.max_message_bytes)
        self._fill(offset + size)
        payload = bytes(self._buffer[offset:offset + size])
        del self._buffer[:offset + size]
        return fin, opcode, payload

    def recv(self) -> Tuple[int, bytes]:
        """The next whole message as ``(OP_TEXT or OP_BINARY, payload)``.

        Raises ``WebSocketTimeout`` (still usable), ``WebSocketClosed`` (the
        peer closed) or ``WebSocketError`` (the connection is gone)."""
        if self.closed:
            raise WebSocketClosed(self.close_code, self.close_reason)
        while True:
            fin, opcode, payload = self._next_frame()
            if opcode == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except WebSocketError:
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code: Optional[int] = None
                reason = ""
                if len(payload) == 1:
                    raise self._fail(CLOSE_PROTOCOL_ERROR, "the server sent a one-byte close payload")
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    reason = payload[2:].decode("utf-8", "replace")
                self.close_code, self.close_reason = code, reason
                self._send_close(code if code is not None else None, "")
                self._drop()
                raise WebSocketClosed(code, reason)
            if opcode == OP_CONTINUATION:
                if self._fragments is None:
                    raise self._fail(CLOSE_PROTOCOL_ERROR, "the server sent an unexpected continuation")
                self._fragments += payload
                if not fin:
                    continue
                message = bytes(self._fragments)
                opcode = self._fragment_opcode
                self._fragments = None
            else:
                if self._fragments is not None:
                    raise self._fail(CLOSE_PROTOCOL_ERROR, "the server started a message inside another")
                if not fin:
                    self._fragments = bytearray(payload)
                    self._fragment_opcode = opcode
                    continue
                message = payload
            if opcode == OP_TEXT:
                try:
                    message.decode("utf-8")
                except UnicodeDecodeError:
                    raise self._fail(CLOSE_INVALID_PAYLOAD, "the server sent text that is not UTF-8")
            return opcode, message


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------- Canvas video messages

def parse_video_message(data: bytes) -> Tuple[Any, ...]:
    """``("session", w, h)``, ``("config", bytes)``, ``("packet", key, pts, data)`` or ``("unknown", type)``."""
    if not data:
        return ("unknown", None)
    kind = data[0]
    if kind == VIDEO_SESSION and len(data) >= 9:
        width, height = struct.unpack("!II", bytes(data[1:9]))
        return ("session", width, height)
    if kind == VIDEO_CONFIG:
        return ("config", bytes(data[1:]))
    if kind == VIDEO_PACKET and len(data) >= 10:
        pts = struct.unpack("!Q", bytes(data[2:10]))[0]
        return ("packet", data[1] != 0, pts, bytes(data[10:]))
    return ("unknown", kind)


def codec_from_sps(data: Optional[bytes]) -> Optional[str]:
    """``avc1.PPCCLL`` from the first SPS NAL unit in Annex B ``data`` (as the Canvas page), else None."""
    if not data:
        return None
    size = len(data)
    index = 0
    while index + 3 < size:
        if data[index] == 0 and data[index + 1] == 0 and data[index + 2] == 1 and (data[index + 3] & 31) == 7:
            out = []
            zeros = 0
            position = index + 3
            while position < size and len(out) < 4:
                byte = data[position]
                position += 1
                if zeros >= 2 and byte == 3:
                    zeros = 0
                    continue
                zeros = zeros + 1 if byte == 0 else 0
                out.append(byte)
            if len(out) < 4:
                return None
            return "avc1." + "".join("%02X" % item for item in out[1:4])
        index += 1
    return None
