#!/usr/bin/env python3
"""Live smoke of the on-device Android helper, protocol 1 (TEST-010).

Not part of the unit suite. It drives one private bench emulator and nothing
else, through its own minimal socket client (no Autonom CLI):

    python3 tests/live/android_helper_smoke.py --serial emulator-5612 \\
        --avd AB_Bench_MCP_API36 [--boot] --evidence-dir DIR

Order of work:

1. Refuse unless ``adb -s SERIAL emu avd name`` is --avd. A serial that is
   not running is booted only with --boot: cold (``-no-snapshot``) on the
   serial's console port, and then it must name --avd too.
2. Wait until the host load (1 minute) is below --max-load (60).
3. Build the helper with ``autonom_lib.android_helper_build`` into a
   temporary AUTONOM_HOME (never the user's state), push it as
   ``/data/local/tmp/autonom-helper-<build>.dex``, forward a fresh port
   (``forward tcp:0 localabstract:autonom-helper``) and start it detached
   under ``app_process`` with a short --idle-ms.
4. Before the helper starts, Settings is restarted on its list and
   "Network & internet" is opened once with ``adb shell input`` and closed
   with BACK (``preflight.settings_warm_up`` in the report), so the helper's
   navigation tap does not also measure the first visit to that page after a
   boot. The reference ``uiautomator dump`` is taken after that, on a still
   list.
5. Before the timing checks (tap_settle, key_back, swipe_scrolls,
   type_unicode, type_insert, set_text_unicode) the device must be warm:
   helper ``read`` calls on the current screen are repeated until two in a
   row report ``settled`` true with ``settle_ms`` <= 1000, for at most 240 s
   (``preflight.device_warm``: ok, seconds, attempts, and the ``busy`` of the
   last read that did not settle). A cold-booted emulator keeps the screen
   busy for a while (system UI, first-run work), and a settle that hits its
   2000 ms limit then says nothing about the helper. A device that never
   warms up is recorded and the checks still run.

Checks (each one is decided from replies and independent reads):

- ``build``: a dex for this source and toolchain.
- ``stale_build_replaced``: a helper started with a wrong build id says so
  in hello, ``quit`` ends it, and the right one starts after it.
- ``hello``: protocol 1, the expected build, UiAutomation connected, the pid
  is the device's ``autonom-helper``.
- ``read_parity``: the helper's XML equals the ``uiautomator dump`` taken
  just before the start in window set and in every node's attributes (an
  outline-level comparison, node by node).
- ``hello_required``: a fresh connection that starts without hello is refused.
- ``tap_settle``: a tap on "Network & internet" with ``read_after`` reports
  a first event and a settled screen, and its read shows that page.
- ``duplicate_id``: the same id again — as an exact replay and aimed at the
  "Internet" row — is refused with ``duplicate_id``, and the page is unchanged
  (same windows, view ids and switch states, still the network page), so no
  second tap reached the device.
- ``key_back`` and ``swipe_scrolls``: BACK returns to the Settings list; a
  swipe scrolls it and a swipe back returns.
- ``type_unicode``: ``type`` of "Привет café 東京 😀" into the focused Settings
  search field reads back exactly, and the reply says ``complete`` true with
  that ``text_after``; ``type_insert``: after ``set_text`` "abc", ``type``
  "é東" reads back "abcé東" (``complete`` true, same ``text_after``);
  ``set_text_unicode``: ``set_text`` of the same text reads back exactly and
  as ``text_after``, ``complete`` true. (No check makes a part of a ``type``
  fail to land on purpose: nothing on the stock bench image does that
  deterministically, and a flaky check would prove nothing.)
- ``keyboard_window``: the windows list holds an input-method window while
  the search field has focus.
- ``screenshot``: a JPEG whose long edge is at most 1000 px and matches the
  reply, with the screen size of the display.
- ``idle_exit``: with no request for --idle-ms the process is gone (not
  before), and its log says ``exit idle``; ``uiautomator_after``: a complete
  ``uiautomator dump`` afterwards.

Cleanup, always: the helper is stopped if still alive (quit, then a kill of
its pid only when ``/proc/<pid>/cmdline`` starts with ``autonom-helper``),
the forward is removed by its exact port (never ``--remove-all``), the
helper's dex and log are removed from /data/local/tmp, every forward listed
before the run is checked to be listed after, and the emulator is stopped
only when this run booted it.

Writes one report, <evidence-dir>/android-helper-smoke.json (plus the dumps,
the helper log and the screenshot beside it), and prints one JSON line
naming it (and ``device_warm``). Exit 0 when every check and the cleanup
passed; 3 (environment fault) when the device never warmed up and every
failed check is a timing check, with the cleanup and the other checks
passing; 1 for any other failure; 2 for a usage error or a refusal (which
touch nothing). SIGTERM or SIGHUP stops the checks, runs the cleanup and
exits 128 + the signal.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import android_helper_build  # noqa: E402

SOCKET = "autonom-helper"
PROCESS = "autonom-helper"
MAIN_CLASS = "dev.autonom.helper.Main"
DEVICE_DIR = "/data/local/tmp"
DEVICE_LOG = f"{DEVICE_DIR}/autonom-helper.log"
SETTINGS = "com.android.settings"
SETTINGS_MAIN = "com.android.settings/.Settings"
UNICODE = "Привет café 東京 😀"
SERIAL = re.compile(r"^emulator-(\d+)$")
AVD = re.compile(r"^[A-Za-z0-9._-]+$")
REPORT_NAME = "android-helper-smoke.json"
EXPECTED_CHECKS = (
    "build", "stale_build_replaced", "hello", "read_parity", "hello_required", "tap_settle",
    "duplicate_id", "key_back", "swipe_scrolls", "keyboard_window", "type_unicode",
    "type_insert", "set_text_unicode", "screenshot", "idle_exit", "uiautomator_after",
)
TIMING_CHECKS = ("tap_settle", "key_back", "swipe_scrolls", "type_unicode", "type_insert",
                 "set_text_unicode")
WARM_LIMIT_S = 240.0
WARM_SETTLE_MS = 1000
WARM_READS = 2
EXIT_ENVIRONMENT = 3
HOME_LABELS = {"Network & internet", "Connected devices"}
NETWORK_LABEL = "Airplane mode"


class StepFailed(Exception):
    pass


class Refused(Exception):
    pass


class Terminated(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host_load() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


class Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def need(self, seconds: float, what: str) -> None:
        if self.left() < seconds:
            raise StepFailed(f"not enough time left for {what}")


# --- adb ---------------------------------------------------------------------------

class Adb:
    """Every device command names the one serial; host commands only read."""

    def __init__(self, adb: str, serial: str) -> None:
        self.adb = adb
        self.serial = serial
        self.calls: list[list[str]] = []

    def run(self, *args: str, timeout: float = 60, check: bool = True
            ) -> subprocess.CompletedProcess:
        argv = [self.adb, "-s", self.serial, *args]
        self.calls.append(argv[1:])
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                                   stdin=subprocess.DEVNULL, text=True, errors="replace")
        if check and completed.returncode != 0:
            raise StepFailed(f"adb {' '.join(args[:4])} failed ({completed.returncode}): "
                             f"{(completed.stderr or completed.stdout or '')[-300:]}")
        return completed

    def shell(self, command: str, *, timeout: float = 60, check: bool = True) -> str:
        return self.run("shell", command, timeout=timeout, check=check).stdout

    def devices(self) -> dict[str, str]:
        """serial -> state for every attached device (a host read)."""
        completed = subprocess.run([self.adb, "devices"], capture_output=True, text=True,
                                   timeout=30, check=False, stdin=subprocess.DEVNULL)
        rows: dict[str, str] = {}
        for line in completed.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows[parts[0]] = parts[1]
        return rows

    def forwards(self) -> list[str]:
        """Every forward of the adb server (a host read), one line each."""
        completed = self.run("forward", "--list", timeout=30, check=False)
        return sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def avd_name(self) -> str | None:
        completed = self.run("emu", "avd", "name", timeout=30, check=False)
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        return lines[0] if completed.returncode == 0 and lines else None

    def helper_pids(self) -> list[int]:
        out = self.shell(f"pidof {PROCESS}", check=False).split()
        return [int(value) for value in out if value.isdigit()]

    def is_helper(self, pid: int) -> bool:
        cmdline = self.shell(f"cat /proc/{pid}/cmdline", check=False)
        return cmdline.split("\0")[0] == PROCESS

    def dump(self) -> tuple[str | None, str]:
        """`uiautomator dump` XML, or None and what it printed instead."""
        completed = self.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=90,
                             check=False)
        out = completed.stdout or ""
        start, end = out.find("<?xml"), out.rfind("</hierarchy>")
        if start < 0 or end < 0:
            return None, (out + (completed.stderr or ""))[-300:]
        return out[start:end + len("</hierarchy>")], ""


# --- XML ---------------------------------------------------------------------------

def parse(xml: str) -> tuple[dict[str, str], list[tuple[int, dict[str, str]]]]:
    root = ET.fromstring(xml.encode("utf-8"))
    nodes: list[tuple[int, dict[str, str]]] = []

    def walk(element: ET.Element, depth: int) -> None:
        for child in element:
            if child.tag == "node":
                nodes.append((depth, dict(child.attrib)))
                walk(child, depth + 1)

    walk(root, 0)
    return dict(root.attrib), nodes


def outline(nodes: list[tuple[int, dict[str, str]]]) -> list[str]:
    return ["  " * depth + " ".join(f"{key}={value!r}" for key, value in attrs.items())
            for depth, attrs in nodes]


def labels(xml: str) -> set[str]:
    return {value for _, attrs in parse(xml)[1] for key in ("text", "content-desc")
            if (value := attrs.get(key))}


def window_set(xml: str) -> list[str]:
    """The package of each top-level node: the windows the XML covers."""
    root = ET.fromstring(xml.encode("utf-8"))
    return [child.get("package", "") for child in root if child.tag == "node"]


_BOUNDS = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")


def center(bounds: str) -> tuple[int, int]:
    match = _BOUNDS.match(bounds or "")
    if not match:
        raise StepFailed(f"unreadable bounds {bounds!r}")
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left + right) // 2, (top + bottom) // 2


def find_target(xml: str, match: Callable[[ET.Element], bool]) -> tuple[ET.Element, tuple[int, int]]:
    """The first node `match` accepts and the centre of its nearest
    clickable ancestor-or-self."""
    root = ET.fromstring(xml.encode("utf-8"))
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in root.iter("node"):
        if match(node):
            target = node
            while target is not None and target.tag == "node" and target.get("clickable") != "true":
                target = parents.get(target)
            chosen = target if target is not None and target.tag == "node" else node
            return node, center(chosen.get("bounds", ""))
    raise StepFailed("no matching node on screen")


def page_signature(xml: str) -> dict[str, list]:
    """What a stray tap would change on a still page: the windows, the set of
    view ids (another page) and the checked state of every checkable node (a
    switch). Text is left out: summaries such as the Wi-Fi name may still
    load after the page settled."""
    root = ET.fromstring(xml.encode("utf-8"))
    nodes = list(root.iter("node"))
    return {
        "windows": window_set(xml),
        "resource_ids": sorted({node.get("resource-id") for node in nodes if node.get("resource-id")}),
        "checked": [node.get("checked") for node in nodes if node.get("checkable") == "true"],
    }


def focused_edit(xml: str) -> dict[str, str] | None:
    for _, attrs in parse(xml)[1]:
        if attrs.get("focused") == "true" and attrs.get("class", "").endswith("EditText"):
            return attrs
    return None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + length
    return None


# --- the helper ----------------------------------------------------------------------

class Client:
    """One connection to the helper through the forward: JSON lines."""

    def __init__(self, port: int, timeout: float = 2.0) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.reader = self.sock.makefile("rb")
        self.log: list[dict[str, Any]] = []

    def request(self, op: str, *, request_id: str | None = None, timeout: float = 8.0,
                **args: Any) -> dict[str, Any]:
        rid = request_id or uuid.uuid4().hex
        line = json.dumps({"id": rid, "op": op, **args}, ensure_ascii=False) + "\n"
        started = time.monotonic()
        self.sock.settimeout(timeout)
        self.sock.sendall(line.encode("utf-8"))
        raw = self.reader.readline()
        elapsed = round((time.monotonic() - started) * 1000, 1)
        if not raw:
            self.log.append({"op": op, "id": rid, "eof": True, "ms": elapsed})
            raise EOFError(f"{op}: the helper closed the connection")
        reply = json.loads(raw.decode("utf-8"))
        self.log.append({"op": op, "id": rid, "ok": reply.get("ok"),
                         "error": (reply.get("error") or {}).get("code"), "ms": elapsed})
        if reply.get("id") != rid:
            raise StepFailed(f"{op}: reply id {reply.get('id')!r} is not {rid!r}")
        reply["_ms"] = elapsed
        return reply

    def close(self) -> None:
        for closer in (self.reader.close, self.sock.close):
            try:
                closer()
            except OSError:
                pass


def ok_reply(reply: dict[str, Any], op: str) -> dict[str, Any]:
    if reply.get("ok") is not True:
        raise StepFailed(f"{op} failed: {reply.get('error')}")
    return reply


# --- the run ---------------------------------------------------------------------------

class Smoke:
    def __init__(self, args: argparse.Namespace, report: dict[str, Any]) -> None:
        self.args = args
        self.report = report
        self.checks: dict[str, dict[str, Any]] = report["checks"]
        self.evidence = Path(args.evidence_dir)
        self.budget = Budget(args.time_limit)
        self.adb = Adb(args.adb, args.serial)
        self.port: int | None = None
        self.build: str | None = None
        self.dex_device: str | None = None
        self.client: Client | None = None
        self.booted: subprocess.Popen | None = None
        self.home = Path(tempfile.mkdtemp(prefix="autonom-helper-smoke-"))
        self.last_request = time.monotonic()
        self.helper_pid: int | None = None
        self.tap_id: str | None = None
        self.tap_point: tuple[int, int] | None = None
        self.network_xml: str | None = None
        self.search_field: dict[str, str] | None = None

    # -- bookkeeping

    def check(self, name: str, ok: bool, **detail: Any) -> bool:
        self.checks[name] = {"ok": bool(ok), **detail}
        return bool(ok)

    def guarded(self, name: str, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception as exc:  # one broken step must not stop the others or the cleanup
            self.checks.setdefault(name, {})
            self.checks[name].update({"ok": False, "error": f"{type(exc).__name__}: {exc}"[-500:]})

    def save(self, name: str, data: str | bytes) -> str:
        path = self.evidence / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return str(path)

    def request(self, op: str, **args: Any) -> dict[str, Any]:
        if self.client is None:
            raise StepFailed("no helper connection")
        try:
            return self.client.request(op, **args)
        finally:
            self.last_request = time.monotonic()

    def read(self) -> dict[str, Any]:
        return ok_reply(self.request("read"), "read")

    # -- device and host

    def resolve_target(self) -> None:
        state = self.adb.devices().get(self.args.serial)
        self.report["preflight"] = preflight = {"state_before": state, "load_start": host_load()}
        if state is not None:
            name = self.adb.avd_name()
            preflight["avd_name"] = name
            if name != self.args.avd:
                raise Refused(f"{self.args.serial} runs AVD {name!r}, not {self.args.avd!r}")
            if state != "device":
                raise Refused(f"{self.args.serial} is {state}")
            preflight["booted_by_smoke"] = False
            return
        if not self.args.boot:
            raise Refused(f"{self.args.serial} is not running; pass --boot to boot "
                          f"{self.args.avd} cold on it")
        self.wait_load("boot")
        self.boot()

    def boot(self) -> None:
        port = int(SERIAL.match(self.args.serial).group(1))
        emulator = self.args.emulator or find_emulator(self.args.adb)
        if not emulator:
            raise StepFailed("emulator binary not found (pass --emulator)")
        argv = [emulator, "-avd", self.args.avd, "-port", str(port), "-no-snapshot",
                "-no-audio", "-no-boot-anim"]
        if not self.args.window:
            argv.append("-no-window")
        log = (self.evidence / "emulator.log").open("w", encoding="utf-8")
        started = time.monotonic()
        self.booted = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.report["boot"] = boot = {"argv": argv, "pid": self.booted.pid, "started_at": now()}
        deadline = time.monotonic() + min(self.args.boot_timeout, self.budget.left() - 300)
        named = False
        while time.monotonic() < deadline:
            if self.booted.poll() is not None:
                raise StepFailed(f"the emulator exited with {self.booted.returncode} "
                                 f"(see emulator.log)")
            if self.adb.devices().get(self.args.serial) == "device":
                if not named:
                    name = self.adb.avd_name()
                    if name is None:
                        time.sleep(2)
                        continue
                    if name != self.args.avd:
                        # another emulator holds the port: stop only our own child
                        self.stop_child()
                        self.booted = None
                        raise StepFailed(f"{self.args.serial} came up as {name!r}")
                    named = True
                if self.adb.shell("getprop sys.boot_completed", check=False).strip() == "1":
                    break
            time.sleep(2)
        else:
            raise StepFailed("the emulator did not finish booting in time")
        # the package manager and the launcher settle after boot_completed
        while time.monotonic() < deadline:
            if self.adb.run("shell", "pm", "path", SETTINGS, check=False).returncode == 0:
                break
            time.sleep(2)
        time.sleep(10)
        boot["seconds"] = round(time.monotonic() - started, 1)
        self.report["preflight"]["booted_by_smoke"] = True

    def wait_load(self, before: str) -> None:
        waited = time.monotonic()
        limit = min(self.args.load_wait, max(0.0, self.budget.left() - 400))
        while True:
            load = host_load()
            if load is None or load < self.args.max_load:
                self.report.setdefault("load", {})[before] = {
                    "load": load, "waited_s": round(time.monotonic() - waited, 1)}
                return
            if time.monotonic() - waited > limit:
                raise StepFailed(f"host load {load} stayed above {self.args.max_load}")
            time.sleep(10)

    def still_home(self, seconds: float) -> str | None:
        """The uiautomator XML of the Settings list once two dumps in a row
        have the same outline, or None after `seconds`."""
        previous = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(1.5)
            xml, _ = self.adb.dump()
            if xml is None or not HOME_LABELS <= labels(xml):
                previous = None
                continue
            if previous is not None and outline(parse(previous)[1]) == outline(parse(xml)[1]):
                return xml
            previous = xml
        return None

    def warm_settings(self, home: str) -> None:
        """Opens "Network & internet" once with adb input and goes back, so
        the helper's navigation tap does not also measure Settings' first
        visit to that page right after a boot. Recorded in the report; a
        failed warm-up is not a failed check."""
        started = time.monotonic()
        detail: dict[str, Any] = {"opened": False}
        self.report["preflight"]["settings_warm_up"] = detail
        try:
            _, (x, y) = find_target(home, lambda node: node.get("text") == "Network & internet")
        except StepFailed as exc:
            detail["error"] = str(exc)
            return
        self.adb.shell(f"input tap {x} {y}", check=False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(1.0)
            page, _ = self.adb.dump()
            if page and NETWORK_LABEL in labels(page):
                detail["opened"] = True
                break
        if detail["opened"]:
            self.adb.shell("input keyevent 4", check=False)
        detail["seconds"] = round(time.monotonic() - started, 1)

    def settings_home(self) -> str:
        """Settings restarted on its list and warmed once; the uiautomator XML
        of a still screen."""
        for _attempt in range(3):
            self.adb.shell(f"am force-stop {SETTINGS}", check=False)
            self.adb.run("shell", "am", "start", "-W", "-n", SETTINGS_MAIN, timeout=120)
            xml = self.still_home(40)
            if xml is None:
                continue
            if "settings_warm_up" not in self.report["preflight"]:
                self.warm_settings(xml)
                xml = self.still_home(40)
                if xml is None:
                    continue
            return xml
        raise StepFailed("Settings did not show a still main list")

    # -- helper lifecycle

    def start_helper(self, build: str, idle_ms: int) -> None:
        command = (f"CLASSPATH={self.dex_device} nohup app_process /system/bin "
                   f"--nice-name={PROCESS} {MAIN_CLASS} --build {build} --idle-ms {idle_ms} "
                   f"--socket {SOCKET} >{DEVICE_LOG} 2>&1 &")
        self.adb.shell(command, timeout=30)

    def connect(self, expect_build: str, budget_s: float = 5.0) -> tuple[Client, dict[str, Any]]:
        """A connection whose hello answered, polled for up to `budget_s`."""
        deadline = time.monotonic() + budget_s
        last = "no answer"
        while time.monotonic() < deadline:
            try:
                client = Client(self.port)
            except OSError as exc:
                last = str(exc)
                time.sleep(0.1)
                continue
            try:
                reply = client.request("hello", timeout=2.0)
                self.last_request = time.monotonic()
                if reply.get("ok") and reply.get("build") == expect_build:
                    return client, reply
                client.close()
                if reply.get("ok"):
                    return None, reply  # type: ignore[return-value]
                last = json.dumps(reply.get("error"))
            except (EOFError, OSError, ValueError) as exc:
                last = str(exc)
                client.close()
            time.sleep(0.1)
        raise StepFailed(f"no hello within {budget_s} s: {last}")

    def wait_gone(self, pid: int, seconds: float) -> float | None:
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if pid not in self.adb.helper_pids():
                return time.monotonic() - started
            time.sleep(0.5)
        return None

    # -- the checks

    def run(self) -> None:
        self.resolve_target()
        self.wait_load("checks")
        self.report["forwards_before"] = self.adb.forwards()
        self.guarded("build", self.step_build)
        if not self.checks.get("build", {}).get("ok"):
            return
        leftovers = self.adb.helper_pids()
        for pid in leftovers:  # an earlier run's helper on this bench device
            if self.adb.is_helper(pid):
                self.adb.shell(f"kill {pid}", check=False)
        self.report["leftover_helpers_stopped"] = leftovers
        self.report["uiautomator_before"] = self.save("uiautomator-before.xml", self.settings_home())
        self.guarded("stale_build_replaced", self.step_stale_build)
        self.guarded("hello", self.step_hello)
        if not self.checks.get("hello", {}).get("ok"):
            return
        self.guarded("read_parity", self.step_read_parity)
        self.guarded("hello_required", self.step_hello_required)
        self.wait_device_warm()
        self.guarded("tap_settle", self.step_tap)
        self.guarded("duplicate_id", self.step_duplicate)
        self.guarded("key_back", self.step_back)
        self.guarded("swipe_scrolls", self.step_swipe)
        self.guarded("keyboard_window", self.step_focus_search)
        self.guarded("type_unicode", self.step_type)
        self.guarded("type_insert", self.step_insert)
        self.guarded("set_text_unicode", self.step_set_text)
        self.guarded("screenshot", self.step_screenshot)
        self.leave_search()
        self.guarded("idle_exit", self.step_idle)
        self.guarded("uiautomator_after", self.step_dump_after)

    def step_build(self) -> None:
        started = time.monotonic()
        env = {**os.environ, "AUTONOM_HOME": str(self.home)}
        result = android_helper_build.ensure(adb_path=self.args.adb, env=env)
        seconds = round(time.monotonic() - started, 2)
        detail: dict[str, Any] = {"seconds": seconds, "reason": result.reason, "hint": result.hint,
                                  "build": result.build_id, "built": result.built}
        if result.toolchain:
            detail.update(javac=str(result.toolchain.javac), d8=str(result.toolchain.d8),
                          android_jar=str(result.toolchain.android_jar),
                          javac_version=result.toolchain.javac_version,
                          d8_version=result.toolchain.d8_version)
        if not result.ok:
            self.check("build", False, **detail)
            return
        detail["dex_bytes"] = result.dex.stat().st_size
        self.build = result.build_id
        self.dex_device = f"{DEVICE_DIR}/autonom-helper-{self.build}.dex"
        self.adb.run("push", str(result.dex), self.dex_device, timeout=120)
        listed = self.adb.shell(f"ls {DEVICE_DIR}", check=False).split()
        others = [name for name in listed
                  if name.startswith("autonom-helper-") and name.endswith(".dex")
                  and f"{DEVICE_DIR}/{name}" != self.dex_device]
        for name in others:  # other builds of the helper, by exact name
            self.adb.shell(f"rm -f {DEVICE_DIR}/{name}", check=False)
        detail["removed_other_builds"] = others
        out = self.adb.run("forward", "tcp:0", f"localabstract:{SOCKET}", timeout=30).stdout.strip()
        if not out.isdigit():
            raise StepFailed(f"forward printed {out!r}, not a port")
        self.port = int(out)
        detail["port"] = self.port
        self.check("build", True, **detail)

    def step_stale_build(self) -> None:
        stale = "stale-" + (self.build or "")[:10]
        self.start_helper(stale, self.args.idle_ms)
        client, reply = self.connect(stale)
        detail: dict[str, Any] = {"hello_build": reply.get("build"), "protocol": reply.get("protocol")}
        pid = reply.get("pid")
        quit_reply = ok_reply(client.request("quit"), "quit")
        client.close()
        gone = self.wait_gone(int(pid), 10) if isinstance(pid, int) else None
        detail.update(pid=pid, quit_ok=quit_reply.get("ok"),
                      gone_after_s=None if gone is None else round(gone, 2))
        self.check("stale_build_replaced", reply.get("build") == stale and gone is not None, **detail)

    def step_hello(self) -> None:
        self.start_helper(self.build, self.args.idle_ms)
        started = time.monotonic()
        self.client, reply = self.connect(self.build)
        pid = reply.get("pid")
        pids = self.adb.helper_pids()
        self.helper_pid = pid if isinstance(pid, int) else None
        verified = bool(self.helper_pid) and self.adb.is_helper(self.helper_pid)
        self.report["timings"]["start_to_hello_ms"] = round((time.monotonic() - started) * 1000, 1)
        display = ok_reply(self.request("display"), "display")
        self.check("hello", reply.get("protocol") == 1 and reply.get("build") == self.build
                   and reply.get("uiautomation") == "connected" and pids == [pid] and verified,
                   protocol=reply.get("protocol"), build=reply.get("build"), pid=pid,
                   device_pids=pids, cmdline_is_helper=verified, sdk_int=reply.get("sdk_int"),
                   uiautomation=reply.get("uiautomation"), hello_ms=reply.get("_ms"),
                   display={key: display.get(key) for key in ("width", "height", "rotation")})

    def step_read_parity(self) -> None:
        reference = Path(self.report["uiautomator_before"]).read_text(encoding="utf-8")
        reply = self.read()
        self.report["timings"]["read_ms"] = reply["_ms"]
        self.save("helper-read.xml", reply["xml"])
        before_root, before = parse(reference)
        after_root, after = parse(reply["xml"])
        diffs = []
        lines_before, lines_after = outline(before), outline(after)
        for index in range(max(len(lines_before), len(lines_after))):
            left = lines_before[index] if index < len(lines_before) else None
            right = lines_after[index] if index < len(lines_after) else None
            if left != right:
                diffs.append({"node": index, "uiautomator": left, "helper": right})
                if len(diffs) >= 8:
                    break
        windows = reply.get("windows") or []
        self.check("read_parity", before_root == after_root and window_set(reference) == window_set(reply["xml"])
                   and not diffs,
                   uiautomator_nodes=len(before), helper_nodes=len(after),
                   uiautomator_windows=window_set(reference), helper_windows=window_set(reply["xml"]),
                   hierarchy=[before_root, after_root], first_differences=diffs,
                   settled=reply.get("settled"), settle_ms=reply.get("settle_ms"),
                   read_ms=reply["_ms"], display=reply.get("display"),
                   windows=[{key: window.get(key) for key in ("type", "package", "active", "focused")}
                            for window in windows])

    def step_hello_required(self) -> None:
        # one connection at a time: the main one closes, a new one starts without hello
        self.client.close()
        self.client = None
        bare = Client(self.port)
        try:
            refused = bare.request("display")
        finally:
            bare.close()
            self.last_request = time.monotonic()
        self.client, hello = self.connect(self.build)
        self.check("hello_required",
                   refused.get("ok") is False and (refused.get("error") or {}).get("code") == "hello_required"
                   and hello.get("ok") is True,
                   refused=refused.get("error"))

    def wait_device_warm(self) -> None:
        """Helper reads of the current screen until WARM_READS in a row
        settle within WARM_SETTLE_MS, for at most WARM_LIMIT_S. Recorded in
        preflight.device_warm; the checks run either way."""
        started = time.monotonic()
        limit = max(0.0, min(WARM_LIMIT_S, self.budget.left() - 420))
        # full_wait: the device had the whole WARM_LIMIT_S and did not warm up
        # (only then is a timing failure an environment fault)
        detail: dict[str, Any] = {"ok": False, "seconds": 0.0, "attempts": 0, "last_busy": None,
                                  "limit_s": round(limit, 1), "settle_ms_max": WARM_SETTLE_MS,
                                  "full_wait": False}
        self.report.setdefault("preflight", {})["device_warm"] = detail
        in_a_row = 0
        recent: list[dict[str, Any]] = []
        try:
            while True:
                reply = self.read()
                detail["attempts"] += 1
                settled, settle_ms = reply.get("settled"), reply.get("settle_ms")
                recent = (recent + [{"settled": settled, "settle_ms": settle_ms}])[-6:]
                if settled is True and isinstance(settle_ms, int) and settle_ms <= WARM_SETTLE_MS:
                    in_a_row += 1
                else:
                    in_a_row = 0
                    if reply.get("busy") is not None:
                        detail["last_busy"] = reply.get("busy")
                if in_a_row >= WARM_READS:
                    detail["ok"] = True
                    break
                if time.monotonic() - started >= limit:
                    detail["full_wait"] = limit >= WARM_LIMIT_S
                    break
                time.sleep(0.5)
        except Exception as exc:  # a broken read is the checks' business, not the warm-up's
            detail["error"] = f"{type(exc).__name__}: {exc}"[-300:]
        detail["recent_reads"] = recent
        detail["seconds"] = round(time.monotonic() - started, 1)

    def step_tap(self) -> None:
        home = self.read()["xml"]
        _, (x, y) = find_target(home, lambda node: node.get("text") == "Network & internet")
        self.tap_id = uuid.uuid4().hex
        self.tap_point = (x, y)
        reply = ok_reply(self.request("tap", request_id=self.tap_id, x=x, y=y, read_after=True),
                         "tap")
        self.report["timings"]["tap_settle_read_ms"] = reply["_ms"]
        page = (reply.get("read") or {}).get("xml") or ""
        self.network_xml = page
        shown = labels(page) if page else set()
        self.check("tap_settle", reply.get("performed") is True and reply.get("settled") is True
                   and reply.get("first_event_ms") is not None and NETWORK_LABEL in shown,
                   point=[x, y], performed=reply.get("performed"), settled=reply.get("settled"),
                   settle_ms=reply.get("settle_ms"), first_event_ms=reply.get("first_event_ms"),
                   network_page=NETWORK_LABEL in shown, round_trip_ms=reply["_ms"],
                   read_error=reply.get("read_error"), busy=reply.get("busy"))

    def step_duplicate(self) -> None:
        if not (self.network_xml and self.tap_id and self.tap_point
                and NETWORK_LABEL in labels(self.network_xml)):
            raise StepFailed("the tap step did not reach the network page")
        before_xml = self.network_xml
        x, y = self.tap_point
        replay = self.request("tap", request_id=self.tap_id, x=x, y=y, read_after=True)
        _, (ix, iy) = find_target(before_xml, lambda node: node.get("text") == "Internet")
        aimed = self.request("tap", request_id=self.tap_id, x=ix, y=iy)
        time.sleep(1.5)
        after_xml = self.read()["xml"]
        before, after = labels(before_xml), labels(after_xml)
        sig_before, sig_after = page_signature(before_xml), page_signature(after_xml)
        unchanged = sig_before == sig_after and NETWORK_LABEL in after
        codes = [(reply.get("error") or {}).get("code") for reply in (replay, aimed)]
        self.check("duplicate_id", codes == ["duplicate_id", "duplicate_id"]
                   and "performed" not in replay and "performed" not in aimed and unchanged,
                   codes=codes, page_unchanged=unchanged,
                   changed={key: {"before": sig_before[key], "after": sig_after[key]}
                            for key in sig_before if sig_before[key] != sig_after[key]},
                   appeared=sorted(after - before)[:10], vanished=sorted(before - after)[:10])

    def step_back(self) -> None:
        reply = ok_reply(self.request("key", keycode=4, read_after=True), "key")
        shown = labels((reply.get("read") or {}).get("xml") or "")
        self.check("key_back", reply.get("performed") is True and HOME_LABELS <= shown,
                   settled=reply.get("settled"), settle_ms=reply.get("settle_ms"),
                   home=HOME_LABELS <= shown, busy=reply.get("busy"))

    def step_swipe(self) -> None:
        display = self.read()["display"]
        width, height = display["width"], display["height"]
        top = labels(self.read()["xml"])
        down = ok_reply(self.request("swipe", x1=width // 2, y1=int(height * 0.75), x2=width // 2,
                                     y2=int(height * 0.3), duration_ms=400, read_after=True),
                        "swipe")
        scrolled = labels(down["read"]["xml"])
        back = ok_reply(self.request("swipe", x1=width // 2, y1=int(height * 0.3), x2=width // 2,
                                     y2=int(height * 0.9), duration_ms=300, read_after=True),
                        "swipe")
        again = labels(back["read"]["xml"])
        for _ in range(3):  # a fling may need another pull to reach the top
            if HOME_LABELS <= again:
                break
            again = labels(ok_reply(self.request(
                "swipe", x1=width // 2, y1=int(height * 0.3), x2=width // 2, y2=int(height * 0.9),
                duration_ms=300, read_after=True), "swipe")["read"]["xml"])
        self.check("swipe_scrolls", scrolled != top and HOME_LABELS <= again,
                   new_labels=sorted(scrolled - top)[:8], back_at_top=HOME_LABELS <= again,
                   swipe_ms=down["_ms"])

    def step_focus_search(self) -> None:
        home = self.read()["xml"]

        def search(node: ET.Element) -> bool:
            words = {(node.get("text") or "").lower(), (node.get("content-desc") or "").lower()}
            rid = node.get("resource-id") or ""
            return "search settings" in words or rid.endswith(":id/search_action_bar")

        _, (x, y) = find_target(home, search)
        ok_reply(self.request("tap", x=x, y=y, read_after=False), "tap")
        field = None
        reply: dict[str, Any] = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reply = self.read()
            field = focused_edit(reply["xml"])
            ime = [w for w in reply.get("windows") or [] if w.get("type") == "input_method"]
            if field and ime:
                break
            time.sleep(0.5)
        self.search_field = field
        ime = [w for w in reply.get("windows") or [] if w.get("type") == "input_method"]
        self.check("keyboard_window", bool(field) and bool(ime),
                   field={key: (field or {}).get(key) for key in ("resource-id", "package", "text")},
                   input_method_windows=ime)

    def field_text(self) -> str | None:
        field = focused_edit(self.read()["xml"])
        return None if field is None else field.get("text")

    def step_type(self) -> None:
        if not self.search_field:
            raise StepFailed("the search field did not get focus")
        reply = ok_reply(self.request("type", text=UNICODE), "type")
        self.report["timings"]["type_ms"] = reply["_ms"]
        time.sleep(0.5)
        text = self.field_text()
        windows = self.read().get("windows") or []
        self.check("type_unicode", reply.get("performed") is True and text == UNICODE
                   and reply.get("complete") is True and reply.get("text_after") == UNICODE,
                   method=reply.get("method"), complete=reply.get("complete"),
                   text_after=reply.get("text_after"), read_back=text, type_ms=reply["_ms"],
                   keyboard_after_typing=any(w.get("type") == "input_method" for w in windows))

    def step_insert(self) -> None:
        cleared = ok_reply(self.request("set_text", text="abc"), "set_text")
        typed = ok_reply(self.request("type", text="é東"), "type")
        time.sleep(0.3)
        text = self.field_text()
        self.check("type_insert", cleared.get("text_after") == "abc" and text == "abcé東"
                   and typed.get("complete") is True and typed.get("text_after") == "abcé東",
                   text_after_set=cleared.get("text_after"), method=typed.get("method"),
                   complete=typed.get("complete"), text_after=typed.get("text_after"),
                   read_back=text)

    def step_set_text(self) -> None:
        reply = ok_reply(self.request("set_text", text=UNICODE), "set_text")
        self.report["timings"]["set_text_ms"] = reply["_ms"]
        time.sleep(0.3)
        text = self.field_text()
        self.check("set_text_unicode", reply.get("text_after") == UNICODE and text == UNICODE
                   and reply.get("complete") is True,
                   text_after=reply.get("text_after"), complete=reply.get("complete"),
                   read_back=text, set_text_ms=reply["_ms"])

    def step_screenshot(self) -> None:
        display = ok_reply(self.request("display"), "display")
        reply = ok_reply(self.request("screenshot", max_edge=1000), "screenshot")
        self.report["timings"]["screenshot_ms"] = reply["_ms"]
        data = base64.b64decode(reply.get("data_base64") or "")
        self.save("helper-screenshot.jpg", data)
        size = jpeg_size(data)
        self.check("screenshot", reply.get("format") == "jpeg" and size is not None
                   and max(size) <= 1000 and list(size) == [reply.get("width"), reply.get("height")]
                   and data.endswith(b"\xff\xd9")
                   and [reply.get("screen_width"), reply.get("screen_height")]
                   == [display.get("width"), display.get("height")],
                   jpeg_size=list(size) if size else None, bytes=len(data),
                   reply_size=[reply.get("width"), reply.get("height")],
                   screen=[reply.get("screen_width"), reply.get("screen_height")],
                   screenshot_ms=reply["_ms"])

    def leave_search(self) -> None:
        if self.client is None:
            return
        for _ in range(2):
            try:
                self.request("key", keycode=4, settle=False)
            except (StepFailed, EOFError, OSError, ValueError):
                break

    def step_idle(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        pid = self.helper_pid
        if not pid:
            raise StepFailed("no helper pid")
        idle_s = self.args.idle_ms / 1000
        self.budget.need(idle_s + 60, "the idle wait")
        while time.monotonic() - self.last_request < idle_s - 1.0:
            if pid not in self.adb.helper_pids():
                break
            time.sleep(0.5)
        early = pid not in self.adb.helper_pids()
        gone = None if early else self.wait_gone(pid, 20)
        after = round(time.monotonic() - self.last_request, 1)
        log = self.adb.shell(f"cat {DEVICE_LOG}", check=False)
        self.report["helper_log"] = self.save("helper.log", log)
        self.check("idle_exit", not early and gone is not None and "exit idle" in log,
                   idle_ms=self.args.idle_ms, gone_after_last_request_s=after,
                   exited_early=early, log_says_idle="exit idle" in log)
        if gone is not None:
            self.helper_pid = None

    def step_dump_after(self) -> None:
        xml, problem = None, ""
        for _ in range(3):
            xml, problem = self.adb.dump()
            if xml:
                break
            time.sleep(2)
        nodes = parse(xml)[1] if xml else []
        if xml:
            self.save("uiautomator-after.xml", xml)
        self.check("uiautomator_after", bool(xml) and len(nodes) > 0, nodes=len(nodes),
                   problem=problem or None)

    # -- cleanup

    def cleanup(self) -> None:
        cleanup: dict[str, Any] = {}
        self.report["cleanup"] = cleanup
        if self.client is not None:
            try:
                self.client.request("quit", timeout=2.0)
            except (StepFailed, EOFError, OSError, ValueError):
                pass
            self.client.close()
            self.client = None
        try:
            pids = self.adb.helper_pids() if self.build else []
            killed = []
            for pid in pids:
                if self.wait_gone(pid, 3) is None and self.adb.is_helper(pid):
                    self.adb.shell(f"kill {pid}", check=False)
                    killed.append(pid)
            cleanup["killed_helper_pids"] = killed
            cleanup["helper_running_after"] = bool(self.adb.helper_pids()) if self.build else False
            if "helper_log" not in self.report and self.build:
                log = self.adb.shell(f"cat {DEVICE_LOG}", check=False)
                self.report["helper_log"] = self.save("helper.log", log)
            if self.port is not None:
                removed = self.adb.run("forward", "--remove", f"tcp:{self.port}", timeout=30,
                                       check=False)
                cleanup["forward_removed"] = removed.returncode == 0
            if self.dex_device:
                self.adb.shell(f"rm -f {self.dex_device} {DEVICE_LOG}", check=False)
                left = self.adb.shell(f"ls {DEVICE_DIR}", check=False).split()
                cleanup["device_files_left"] = [name for name in left
                                                if name.startswith("autonom-helper")]
            before = set(self.report.get("forwards_before") or [])
            after = set(self.adb.forwards())
            own = {line for line in before | after
                   if self.port is not None and f" tcp:{self.port} " in f" {line} "}
            cleanup["own_forward_listed_after"] = sorted(own & after)
            cleanup["forwards_missing_after"] = sorted(before - after)
            cleanup["forwards_added_after"] = sorted(after - before - own)
        except (StepFailed, OSError, subprocess.TimeoutExpired) as exc:
            cleanup["error"] = f"{type(exc).__name__}: {exc}"[-300:]
        if self.booted is not None:
            cleanup["emulator_stopped"] = self.stop_emulator()
        shutil.rmtree(self.home, ignore_errors=True)
        cleanup["ok"] = (not cleanup.get("error") and not cleanup.get("helper_running_after")
                         and cleanup.get("forward_removed", True)
                         and not cleanup.get("own_forward_listed_after")
                         and not cleanup.get("forwards_missing_after")
                         and not cleanup.get("device_files_left")
                         and cleanup.get("emulator_stopped", True))

    def stop_emulator(self) -> bool:
        """The emulator this run booted: `emu kill` only while its console
        still names our AVD, else our own child process only."""
        try:
            if self.adb.avd_name() == self.args.avd:
                self.adb.run("emu", "kill", timeout=30, check=False)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.booted.poll() is not None and self.args.serial not in self.adb.devices():
                return True
            time.sleep(1)
        self.stop_child()
        return self.booted.poll() is not None and self.args.serial not in self.adb.devices()

    def stop_child(self) -> None:
        if self.booted is None or self.booted.poll() is not None:
            return
        self.booted.terminate()
        try:
            self.booted.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.booted.kill()
            self.booted.wait(timeout=10)


def find_emulator(adb: str) -> str | None:
    candidates = [os.environ.get("AUTONOM_EMULATOR"),
                  str(Path(adb).resolve().parent.parent / "emulator/emulator")]
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(name):
            candidates.append(str(Path(os.environ[name]) / "emulator/emulator"))
    candidates += [str(Path.home() / "Library/Android/sdk/emulator/emulator"), shutil.which("emulator")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", required=True, help="emulator-<port> of the bench AVD")
    parser.add_argument("--avd", required=True, help="the AVD name the serial must run")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--boot", action="store_true", help="boot the AVD cold when not running")
    parser.add_argument("--window", action="store_true", help="show the emulator window")
    parser.add_argument("--adb", default=os.environ.get("AUTONOM_ADB") or shutil.which("adb"))
    parser.add_argument("--emulator")
    parser.add_argument("--idle-ms", type=int, default=12000)
    parser.add_argument("--max-load", type=float, default=60.0)
    parser.add_argument("--load-wait", type=float, default=600.0)
    parser.add_argument("--boot-timeout", type=float, default=420.0)
    parser.add_argument("--time-limit", type=float, default=1400.0)
    args = parser.parse_args(argv)
    if not SERIAL.match(args.serial):
        parser.error("--serial must be emulator-<port>")
    if not AVD.match(args.avd):
        parser.error("--avd must be a plain AVD name")
    if not args.adb:
        parser.error("adb not found (pass --adb)")
    if not 2000 <= args.idle_ms <= 120000:
        parser.error("--idle-ms must be 2000..120000")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "ok": False, "serial": args.serial, "avd": args.avd, "started_at": now(),
        "idle_ms": args.idle_ms, "expected_checks": list(EXPECTED_CHECKS), "checks": {},
        "timings": {},
    }
    smoke = Smoke(args, report)

    def handler(signum: int, _frame: Any) -> None:
        raise Terminated(signum)

    previous = {signum: signal.signal(signum, handler) for signum in (signal.SIGTERM, signal.SIGHUP)}
    code = 1
    try:
        try:
            smoke.run()
        except Refused as exc:
            report["refused"] = str(exc)
            code = 2
        except Terminated as exc:
            report["terminated"] = signal.Signals(exc.signum).name
            code = 128 + exc.signum
        except (StepFailed, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"[-500:]
        finally:
            for signum in previous:  # the cleanup is not interrupted
                signal.signal(signum, signal.SIG_IGN)
            if "refused" not in report:
                smoke.cleanup()
            else:
                shutil.rmtree(smoke.home, ignore_errors=True)
            for signum, old in previous.items():
                signal.signal(signum, old)
    finally:
        checks = report["checks"]
        missing = [name for name in EXPECTED_CHECKS if name not in checks]
        report["missing_checks"] = missing
        report["load_end"] = host_load()
        report["finished_at"] = now()
        report["adb_calls"] = len(smoke.adb.calls)
        report["adb_serials"] = sorted({call[1] for call in smoke.adb.calls if call[0] == "-s"})
        failed = [name for name, check in checks.items() if not check.get("ok")]
        warm = (report.get("preflight") or {}).get("device_warm")
        report["ok"] = (code == 1 and "error" not in report and not missing and not failed
                        and (report.get("cleanup") or {}).get("ok") is True)
        if report["ok"]:
            code = 0
        elif environment_fault(code, report, failed, missing, warm):
            code = EXIT_ENVIRONMENT
            report["environment_fault"] = ("the device never warmed up and only timing checks "
                                           "failed")
        report["exit_code"] = code
        out = evidence / REPORT_NAME
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "report": str(out), "failed": failed,
                          "missing": missing, "device_warm": warm}, ensure_ascii=False))
    return code


def environment_fault(code: int, report: dict[str, Any], failed: list[str], missing: list[str],
                      warm: Any) -> bool:
    """True for a run whose only failures are timing checks on a device that
    did not warm up in the whole warm-up time: everything else, the cleanup
    included, passed, and no failed check broke off with an exception (an
    error reply or a lost connection is not the device being slow)."""
    checks = report.get("checks") or {}
    return (code == 1 and "error" not in report and not missing and bool(failed)
            and set(failed) <= set(TIMING_CHECKS)
            and not any("error" in (checks.get(name) or {}) for name in failed)
            and isinstance(warm, dict) and warm.get("ok") is False
            and warm.get("full_wait") is True
            and (report.get("cleanup") or {}).get("ok") is True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
