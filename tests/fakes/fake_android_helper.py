"""An in-process stand-in for the on-device Android helper, protocol 1.

A TCP server on 127.0.0.1 that answers JSON lines the way
``native/android/helper`` does (see its README): the first request of a
connection must be ``hello`` (else ``hello_required``), the last 256 request
ids are kept and a repeated one is refused with ``duplicate_id`` without being
performed, one connection is served at a time, and ``quit`` answers ok and
ends the helper. Every request received is kept in ``requests`` (in order,
with its connection number), so a test can count exactly what reached the
helper.

With ``state_path`` (the ``AUTONOM_FAKE_STATE`` file of ``fake_adb.py``) the
fake follows the device: it serves only while ``helper_running`` names a pid
that ``helper_procs`` still lists (the ``app_process`` start of the fake adb
sets it, a ``kill`` of that pid clears it), it reports that start's build and
pid, and ``quit`` removes the pid. Without one it serves ``build``/``pid``
until ``quit``. A dead helper behind a still-listed forward accepts a
connection and closes it at once: the client sees end of stream, as on a
device.

Fault modes (attributes; counts are used up one per connection or request):

``eof_on_hello``     N: the next N connections read the hello and close
``wrong_build``      hello reports this build instead
``busy_hellos``      N: the next N hellos answer ``uiautomation_busy``, then the helper exits
``failed_hellos``    N: the same with ``uiautomation_failed``
``drop_after``       {op: N}: the next N requests of that op are read and the
                     connection closed with no reply (N < 0: always)
``hung_hellos``      N: the next N hellos are read and never answered; the
                     connection stays open until the client closes it (N < 0:
                     always). A stuck helper, as a caller holding the use lock sees it
``hung``             {op: N}: the next N requests of that op are read and never
                     answered (N < 0: always), and the helper process that got
                     one is wedged: it answers nothing more on any connection
                     (a hello included) until it is killed. A new start serves
``slow``             {op: seconds}: wait that long before replying
``replies``          {op: dict}: keys merged into that op's reply
``failing_reads``    N: the next N ``read`` requests answer ``internal`` (not performed)
``failing_read_after`` N: the next N inputs with ``read_after`` are performed and
                     settled, and answer ``read_error`` instead of ``read``

The screen (attributes):

``xml``              what ``read`` (and an input's ``read_after``) returns
``xml_after``        once an input op was performed, the screen changes to this
                     (its ``read_after`` and every later read show it)
``display``          ``{width, height, rotation}`` of reads and ``display``
``windows``          the windows list of reads (``read`` and ``read_after``), the
                     helper's ``windows`` entries (``type``, ``package``, ``bounds``, ...)
``max_keycode``      KeyEvent.getMaxKeyCode() of the device (``key`` above it is refused)

``screenshot`` answers like Shot.java: a JPEG (a baseline start-of-frame
header carrying the size) scaled so that its long edge is at most ``max_edge``
(1000 by default, from 16 to 100000; never upscaled), from a screen of
``display``'s size, with ``screen_width``/``screen_height``; ``quality`` must
be from 1 to 100.

Text fields (``type`` and ``set_text``; README "Typing"). Without ``fields``
both answer as if the text landed. With ``fields`` (a list of dicts:
``bounds`` [l,t,r,b], ``text``, optional ``password``, ``editable`` (default
true) and ``readable`` (default true: false for a field whose node exposes
no text)), the fake keeps them:

``fields``           the fields on screen; a ``tap`` inside one's bounds gives it
                     input focus (unless ``tap_focuses`` is false)
``focused``          the index in ``fields`` of the field with input focus, or None
``tap_focuses``      false: a tap does not move the focus (a missed tap)
``bounds_after_tap`` {index: [l,t,r,b]}: after the next tap, those fields have moved
                     there (the keyboard pushed the layout up); used up by that tap
``keys_drop``        how many trailing characters a ``type`` loses (a part that did
                     not land: ``complete: false``)

``type`` appends at the end of the focused field's text (the cursor) and
answers ``method`` (``keys`` for printable ASCII, ``set_text`` for none of it,
else ``mixed``), ``text_after`` (null for a password field) and ``complete``;
``no_focus``, ``not_editable``, and ``text_not_typable`` for text that needs
set-text in a password field that is not known to be empty (it holds text,
or its node exposes none). As in Input.java (owner decision ODA-002), any
other field is typed into as it reads, and one whose node exposes no text
counts as empty: the text replaces what it held. ``set_text`` replaces the
text after checking ``expect_bounds`` (``fill_target_mismatch``).
Both settle and read only when the request asks (``settle``/``read_after``).

Arguments are checked like ``Main.java`` checks them, before anything is
performed: a ``read``'s ``quiet_ms``/``max_ms`` and a settle block's
``first_event_ms``/``quiet_ms``/``max_ms`` must be whole numbers from 0 to
60000, a gesture's ``duration_ms`` from 1 to 60000 and ``keycode`` from 1 to
``max_keycode``; anything else is ``bad_request``, not performed.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from collections import deque
from typing import Any

FAKES = Path(__file__).resolve().parent
XML = (FAKES.parent / "fixtures/ui_dump.xml")
# A JPEG's start and end markers: the smallest fake screenshot.
JPEG = b"\xff\xd8\xff\xd9"


def jpeg_bytes(width: int, height: int) -> bytes:
    """A JPEG whose baseline start-of-frame (SOF0) says ``width`` x
    ``height``, after an APP0 (JFIF) segment as a camera or the platform
    encoder writes one: what a size reader must find."""
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big")
            + width.to_bytes(2, "big") + b"\x03" + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"
INPUT_OPS = ("tap", "long_press", "swipe", "key")
# `_answer`'s result for a request read and never answered.
_SILENT = object()
MAX_WAIT_MS = 60000  # Main.MAX_WAIT_MS


def _bad_int(args: dict, key: str, low: int, high: int) -> str | None:
    """Main.intArg: absent is the default; otherwise a whole number in range."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{key} must be a whole number"
    if value != int(value) or not low <= value <= high:
        return f"{key} must be a whole number from {low} to {high}"
    return None


def _load(path: Path) -> dict:
    for _ in range(200):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            time.sleep(0.01)
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, state: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                             dir=str(path.parent))
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2) + "\n")
    os.replace(temporary, path)


class FakeHelper:
    def __init__(self, *, build: str = "0123456789abcdef", pid: int = 4242, sdk_int: int = 36,
                 state_path: str | os.PathLike | None = None, xml: str | None = None) -> None:
        self.build = build
        self.pid = pid
        self.sdk_int = sdk_int
        self.state_path = Path(state_path) if state_path else None
        self.xml = xml if xml is not None else XML.read_text(encoding="utf-8")
        self.alive = True  # standalone mode only
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.ids: deque[str] = deque(maxlen=256)
        self.eof_on_hello = 0
        self.wrong_build: str | None = None
        self.busy_hellos = 0
        self.failed_hellos = 0
        self.drop_after: dict[str, int] = {}
        self.hung_hellos = 0
        self.hung: dict[str, int] = {}
        self.wedged: set[int] = set()  # pids that took a hung request
        self.slow: dict[str, float] = {}
        self.replies: dict[str, dict[str, Any]] = {}
        self.failing_reads = 0
        self.failing_read_after = 0
        self.xml_after: str | None = None
        self.display = {"width": 1080, "height": 2400, "rotation": 0}
        self.windows: list[dict[str, Any]] = []
        self.max_keycode = 316
        self.fields: list[dict[str, Any]] = []
        self.focused: int | None = None
        self.tap_focuses = True
        self.bounds_after_tap: dict[int, list[int]] = {}
        self.keys_drop = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.1)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="fake-android-helper",
                                        daemon=True)

    # -- lifecycle

    def start(self) -> "FakeHelper":
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            self._server.close()
        except OSError:
            pass

    def __enter__(self) -> "FakeHelper":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- what the device runs

    def serving(self) -> dict[str, Any] | None:
        """{pid, build} of the helper that serves the socket now, or None."""
        if self.state_path is None:
            return {"pid": self.pid, "build": self.build} if self.alive else None
        state = _load(self.state_path)
        running = state.get("helper_running") or {}
        pid = running.get("pid")
        if pid is None or str(pid) not in (state.get("helper_procs") or {}):
            return None
        return {"pid": int(pid), "build": running.get("build")}

    def _exit(self, pid: int) -> None:
        """The helper process ends (quit, or a refused hello)."""
        if self.state_path is None:
            self.alive = False
            return
        state = _load(self.state_path)
        procs = state.get("helper_procs") or {}
        procs.pop(str(pid), None)
        state["helper_procs"] = procs
        if str((state.get("helper_running") or {}).get("pid")) == str(pid):
            state.pop("helper_running", None)
        _save(self.state_path, state)

    def ops(self, op: str | None = None) -> list[dict[str, Any]]:
        """Requests received (all, or of one op), in order."""
        with self._lock:
            return [dict(item) for item in self.requests if op is None or item.get("op") == op]

    def wait_ops(self, op: str, count: int = 1, timeout: float = 5.0) -> list[dict[str, Any]]:
        """`ops(op)` once at least `count` arrived, or after `timeout` s: the
        serving thread records a request when it reads it, which may be
        after the client gave up on its reply."""
        deadline = time.monotonic() + timeout
        while len(self.ops(op)) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.ops(op)

    # -- serving

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                self._handle(connection)

    def _take(self, name: str) -> bool:
        count = getattr(self, name)
        if count > 0:
            setattr(self, name, count - 1)
            return True
        return False

    def _handle(self, connection: socket.socket) -> None:
        with self._lock:
            self.connections += 1
            number = self.connections
        current = self.serving()
        if current is None:
            return  # a dead helper behind a listed forward: end of stream at once
        connection.settimeout(0.1)
        reader = b""
        greeted = False
        while not self._stop.is_set():
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            reader += chunk
            while b"\n" in reader:
                line, _, reader = reader.partition(b"\n")
                outcome = self._answer(line, current, greeted, number)
                if outcome is _SILENT:
                    continue  # read, never answered; the connection stays open
                if outcome is None:
                    return  # closed without a reply
                reply, greeted, keep = outcome
                try:
                    connection.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                except OSError:
                    return
                if not keep:
                    return

    def _answer(self, line: bytes, current: dict[str, Any], greeted: bool, number: int
                ) -> tuple[dict[str, Any], bool, bool] | object | None:
        try:
            request = json.loads(line.decode("utf-8"))
        except ValueError:
            return {"id": None, "ok": False,
                    "error": {"code": "bad_request", "message": "not JSON"}}, greeted, True
        rid, op = request.get("id"), request.get("op")
        with self._lock:
            self.requests.append({**request, "connection": number})
        if current["pid"] in self.wedged:
            return _SILENT
        if op == "hello" and self.hung_hellos:
            if self.hung_hellos > 0:
                self.hung_hellos -= 1
            return _SILENT
        hangs = self.hung.get(op)
        if hangs:
            if hangs > 0:
                self.hung[op] = hangs - 1
            self.wedged.add(current["pid"])
            return _SILENT
        if op == "hello" and self._take("eof_on_hello"):
            return None
        drops = self.drop_after.get(op)
        if drops:
            if drops > 0:
                self.drop_after[op] = drops - 1
            return None
        delay = self.slow.get(op)
        if delay:
            time.sleep(delay)
        if not isinstance(rid, str) or not rid:
            return {"id": None, "ok": False,
                    "error": {"code": "bad_request", "message": "no id"}}, greeted, True
        if rid in self.ids:
            return self._error(rid, "duplicate_id", "this id was used before"), greeted, True
        self.ids.append(rid)
        if op == "hello":
            base = {"protocol": 1, "build": self.wrong_build or current["build"],
                    "pid": current["pid"], "sdk_int": self.sdk_int}
            for name, code in (("busy_hellos", "uiautomation_busy"),
                               ("failed_hellos", "uiautomation_failed")):
                if self._take(name):
                    self._exit(current["pid"])
                    return {**self._error(rid, code, "UiAutomation is not available"),
                            **base}, False, False
            return {"id": rid, "ok": True, **base, "uiautomation": "connected"}, True, True
        if not greeted:
            return self._error(rid, "hello_required", "send hello first"), greeted, True
        if op == "quit":
            self._exit(current["pid"])
            return {"id": rid, "ok": True}, greeted, False
        body = self._perform(op, request)
        if body is None:
            return self._error(rid, "unknown_op", f"unknown op {op}"), greeted, True
        body.update(self.replies.get(op, {}))
        reply = {"id": rid, "ok": "error" not in body, **body}
        return reply, greeted, True

    @staticmethod
    def _error(rid: Any, code: str, message: str) -> dict[str, Any]:
        return {"id": rid, "ok": False, "error": {"code": code, "message": message}}

    def _read(self) -> dict[str, Any]:
        return {"xml": self.xml, "windows": [dict(item) for item in self.windows],
                "display": dict(self.display)}

    def _after_input(self, request: dict[str, Any], body: dict[str, Any], *,
                     settle_by_default: bool) -> None:
        """Main.perform after the input: the settle (by default for touches and
        keys; for type and set_text only when asked) and the read after it."""
        settle = request.get("settle")
        settle_on = (settle_by_default or bool(request.get("read_after"))
                     if settle is None else settle is not False)
        if settle_on:
            body.update(settled=True, settle_ms=600, first_event_ms=40)
        if request.get("read_after"):
            if self._take("failing_read_after"):
                body["read_error"] = {"code": "internal",
                                      "message": "the read after the input failed"}
            else:
                body["read"] = self._read()

    def _focus_at(self, x: Any, y: Any) -> None:
        """A tap inside a field's bounds gives it input focus."""
        if not self.tap_focuses or not isinstance(x, (int, float)) \
                or not isinstance(y, (int, float)):
            return
        for index, field in enumerate(self.fields):
            left, top, right, bottom = field["bounds"]
            if left <= x <= right and top <= y <= bottom:
                self.focused = index
                return

    @staticmethod
    def _keys_can(char: str) -> bool:
        return " " <= char <= "~"

    def _text_op(self, op: str, request: dict[str, Any]) -> dict[str, Any]:
        """``type`` or ``set_text`` as Input.java performs them on ``fields``."""
        text = request.get("text")
        if not isinstance(text, str):
            return {"error": {"code": "bad_request", "message": "text must be a string"}}
        if op == "type" and not text:
            return {"error": {"code": "bad_request", "message": "text must not be empty"}}
        keys = [self._keys_can(char) for char in text]
        method = "keys" if all(keys) else "set_text" if not any(keys) else "mixed"
        if not self.fields:  # no field model: the text lands as asked
            body: dict[str, Any] = {"performed": True, "text_after": text, "complete": True}
            if op == "type":
                body["method"] = method
            return body
        if self.focused is None or not 0 <= self.focused < len(self.fields):
            return {"error": {"code": "no_focus",
                              "message": "No view has input focus; tap a text field first"}}
        field = self.fields[self.focused]
        if not field.get("editable", True):
            return {"error": {"code": "not_editable",
                              "message": "The focused view is not an editable text field"}}
        # Input.unreadable: a password field only. A field whose node exposes
        # no text reads as empty (Input.currentText), as an empty field does.
        unreadable = bool(field.get("password"))
        hidden = field.get("readable") is False
        if op == "set_text":
            expected = request.get("expect_bounds")
            if expected is not None and list(expected) != list(field["bounds"]):
                left, top, right, bottom = field["bounds"]
                return {"error": {"code": "fill_target_mismatch",
                                  "message": f"The focused field is at [{left},{top}][{right},"
                                             f"{bottom}], not at the expected bounds"}}
            field["text"] = text
            return {"performed": True, "text_after": None if unreadable else text,
                    "complete": None if unreadable else True}
        if unreadable and method != "keys" and (field.get("text") or hidden):
            # Input.readableEmpty: only an empty text (or the hint) is known empty
            return {"error": {"code": "text_not_typable",
                              "message": "The password field may already hold text, which "
                                         "cannot be read, and this text needs set-text"}}
        landed = text[:len(text) - self.keys_drop] if self.keys_drop else text
        if hidden and not unreadable:
            # typed into as empty: what the node did not show is replaced
            field["text"] = landed
            field.pop("readable", None)  # it shows the typed text now
        else:
            field["text"] = str(field.get("text") or "") + landed
        if unreadable:
            return {"performed": True, "method": method, "text_after": None,
                    "complete": False if self.keys_drop else None}
        return {"performed": True, "method": method, "text_after": field["text"],
                "complete": not self.keys_drop}

    def _refusal(self, op: str, request: dict[str, Any]) -> str | None:
        """The bad_request message Main.java would answer, or None."""
        checks: list[tuple[dict, str, int, int]] = []
        if op == "read":
            checks += [(request, "quiet_ms", 0, MAX_WAIT_MS), (request, "max_ms", 0, MAX_WAIT_MS)]
        if op in ("long_press", "swipe"):
            checks.append((request, "duration_ms", 1, MAX_WAIT_MS))
        if op == "key":
            if request.get("keycode") is None:
                return "keycode is required"
            checks.append((request, "keycode", 1, self.max_keycode))
        if op == "screenshot":
            checks += [(request, "max_edge", 16, 100000), (request, "quality", 1, 100)]
        if op in INPUT_OPS or op in ("type", "set_text"):
            settle = request.get("settle")
            if isinstance(settle, dict):
                checks += [(settle, key, 0, MAX_WAIT_MS)
                           for key in ("first_event_ms", "quiet_ms", "max_ms")]
            elif settle is not None and not isinstance(settle, bool):
                return "settle must be an object or a boolean"
        for args, key, low, high in checks:
            message = _bad_int(args, key, low, high)
            if message:
                return message
        return None

    def _perform(self, op: str, request: dict[str, Any]) -> dict[str, Any] | None:
        refused = self._refusal(op, request)
        if refused:
            return {"error": {"code": "bad_request", "message": refused}}
        if op == "read":
            if self._take("failing_reads"):
                return {"error": {"code": "internal", "message": "the read failed"}}
            return {**self._read(), "settled": True, "settle_ms": 20}
        if op == "display":
            return dict(self.display)
        if op in INPUT_OPS:
            if self.xml_after is not None:
                self.xml = self.xml_after  # the input changed the screen
            if op == "tap":
                self._focus_at(request.get("x"), request.get("y"))
                for index, bounds in self.bounds_after_tap.items():
                    self.fields[index]["bounds"] = list(bounds)
                self.bounds_after_tap = {}
            body: dict[str, Any] = {"performed": True}
            self._after_input(request, body, settle_by_default=True)
            return body
        if op in ("type", "set_text"):
            outcome = self._text_op(op, request)
            if "error" in outcome:
                return outcome
            self._after_input(request, outcome, settle_by_default=False)
            return outcome
        if op == "screenshot":
            return self._screenshot(request)
        return None

    def _screenshot(self, request: dict[str, Any]) -> dict[str, Any]:
        """Shot.take: scaled to the long edge (never up), JPEG."""
        width, height = int(self.display["width"]), int(self.display["height"])
        max_edge = int(request.get("max_edge") or 1000)
        long_edge = max(width, height)
        image_width, image_height = width, height
        if long_edge > max_edge:
            factor = max_edge / long_edge
            # Math.round: half up
            image_width = max(1, int(width * factor + 0.5))
            image_height = max(1, int(height * factor + 0.5))
        data = jpeg_bytes(image_width, image_height)
        return {"format": "jpeg", "width": image_width, "height": image_height,
                "screen_width": width, "screen_height": height,
                "data_base64": base64.b64encode(data).decode()}
