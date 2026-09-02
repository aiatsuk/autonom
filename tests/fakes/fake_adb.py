#!/usr/bin/env python3
"""Deterministic stand-in for `adb`, used by Autonom tests.

Records every invocation to ``$AUTONOM_FAKE_LOG`` (one JSON array element per
line) so tests can assert **what was actually executed**, not what the CLI
reported. Canned responses are overridable through ``$AUTONOM_FAKE_STATE``,
a JSON file read fresh on every call.

State keys (all optional):

``devices``         list of ``[serial, state, "key:value ..."]`` rows
``ui_dump``         path to a uiautomator XML file to echo
``logcat``          list of raw logcat lines
``pidof``           mapping of package -> pid string
``settings``        mapping of setting name -> current value
``clock_skew``      seconds the fake device clock lags the host (default 0)
``fail``            mapping of "joined argv prefix" -> [exit_code, message]
``avd_names``       mapping of serial -> AVD name for ``emu avd name``
``run_as_refused``  text run-as prints instead of a listing (system / release app)
``battery_level``   level ``dumpsys battery`` reports (``set level`` updates it)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 1x1 transparent PNG; enough for signature and size assertions.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "00057dd8b7c40000000049454e44ae426082"
)


def load_state() -> dict:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def record(argv: list[str]) -> None:
    path = os.environ.get("AUTONOM_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "adb", "argv": argv}) + "\n")


def write_state(state: dict) -> None:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if path:
        Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    record(argv)
    state = load_state()

    for prefix, outcome in (state.get("fail") or {}).items():
        if " ".join(argv).startswith(prefix):
            code, message = outcome
            sys.stdout.write(message + "\n")
            return int(code)

    # Strip the target selector so command matching is position-independent.
    args = list(argv)
    if len(args) >= 2 and args[0] == "-s":
        args = args[2:]

    if args[:1] == ["version"]:
        sys.stdout.write(state.get("adb_version", "Android Debug Bridge version 1.0.41") + "\n")
        return 0

    if args[:1] == ["devices"]:
        rows = state.get("devices", [["emulator-5554", "device", "product:sdk_gphone64_arm64"]])
        sys.stdout.write("List of devices attached\n")
        for row in rows:
            serial, device_state = row[0], row[1]
            extra = row[2] if len(row) > 2 else ""
            sys.stdout.write(f"{serial}\t{device_state} {extra}\n".rstrip() + "\n")
        return 0

    if args[:3] == ["exec-out", "uiautomator", "dump"]:
        dump = state.get("ui_dump")
        if dump:
            sys.stdout.write(Path(dump).read_text(encoding="utf-8"))
        return 0

    if args[:2] == ["exec-out", "run-as"]:
        # A non-debuggable package: run-as complains on the same stream the
        # listing would use, which is exactly how the complaint became a "file".
        refused = state.get("run_as_refused")
        if refused:
            sys.stdout.write(refused + "\n")
            return 1
        sys.stdout.write("files\nshared_prefs\n")
        return 0

    if args[:2] == ["exec-out", "screencap"]:
        sys.stdout.buffer.write(PNG)
        return 0

    if args[:1] == ["logcat"]:
        for line in state.get("logcat", []):
            sys.stdout.write(line + "\n")
        return 0

    if args[:3] == ["shell", "dumpsys", "battery"]:
        # The battery service remembers `set level`, so a pin can be read back.
        if args[3:5] == ["set", "level"] and len(args) > 5:
            state["battery_level"] = args[5]
            write_state(state)
            return 0
        if args[3:4] in (["set"], ["unplug"], ["reset"]):
            return 0
        sys.stdout.write(
            "Current Battery Service state:\n  AC powered: false\n  USB powered: false\n"
            f"  status: 4\n  level: {state.get('battery_level', '100')}\n")
        return 0

    if args[:3] == ["shell", "dumpsys", "location"]:
        default = (
            "    fused provider:\n"
            "      last location=Location[fused 55.751244,37.618423 hAcc=5.0 et=+1h]\n"
            "    gps provider:\n"
            "      last location=Location[gps 55.751244,37.618423 hAcc=8.0 et=+1h]\n"
            "    network provider:\n"
            "      last location=null\n"
        )
        sys.stdout.write(state.get("dumpsys_location", default))
        return 0

    if args[:2] == ["shell", "date"]:
        # `logs tail` derives its window from the DEVICE clock, so the fake has
        # to have one. `clock_skew` lets a test reproduce the drift that made a
        # real emulator's --since window come back empty.
        spec = (args[2] if len(args) > 2 else "+%s").lstrip("+").replace("_", " ")
        moment = time.time() - float(state.get("clock_skew", 0))
        sys.stdout.write(time.strftime(spec, time.localtime(moment)).replace(" ", "_")
                         if "_" in (args[2] if len(args) > 2 else "")
                         else time.strftime(spec, time.localtime(moment)))
        sys.stdout.write("\n")
        return 0

    if args[:1] == ["root"]:
        # `root_refused` reproduces a Play-store image, where adb root is blocked.
        if state.get("root_refused"):
            sys.stdout.write("adbd cannot run as root in production builds\n")
        else:
            sys.stdout.write("restarting adbd as root\n")
        return 0

    if args[:1] == ["wait-for-device"]:
        return 0

    if args[:3] == ["emu", "geo", "fix"]:
        # `geo fix <lon> <lat>` — success is silent on a real emulator. A
        # `geo_fix_fails` flag lets a test drive the unreachable-console path.
        if state.get("geo_fix_fails"):
            sys.stdout.write("KO: unable to reach the emulator console\n")
            return 1
        sys.stdout.write("OK\n")
        return 0

    if args[:3] == ["emu", "avd", "name"]:
        # The console answers with the AVD name then "OK"; a serial with no
        # mapping answers "OK" alone, which is what a hardware serial does.
        serial = argv[1] if argv[:1] == ["-s"] and len(argv) >= 2 else ""
        name = (state.get("avd_names") or {}).get(serial)
        if name:
            sys.stdout.write(name + "\n")
        sys.stdout.write("OK\n")
        return 0

    if args[:2] == ["emu", "kill"]:
        # The serial travels in the stripped-off selector; consume it from the
        # raw argv so the killed emulator disappears from later `devices` calls.
        killed = argv[1] if argv[:1] == ["-s"] and len(argv) >= 2 else None
        rows = state.get("devices", [["emulator-5554", "device", "product:sdk_gphone64_arm64"]])
        state["devices"] = [row for row in rows if killed is None or row[0] != killed]
        write_state(state)
        sys.stdout.write("OK: killing emulator, bye bye\n")
        return 0

    if args[:2] == ["shell", "getprop"]:
        prop = args[2] if len(args) > 2 else ""
        default = "1" if prop == "sys.boot_completed" else ""
        sys.stdout.write(str((state.get("getprop") or {}).get(prop, default)) + "\n")
        return 0

    if args[:1] == ["push"]:
        return 0

    if args[:1] == ["pull"] and len(args) > 2:
        Path(args[2]).write_bytes(state.get("pull_bytes", "FAKEDATA").encode())
        return 0

    if args[:2] == ["shell", "which"]:
        binary = args[2] if len(args) > 2 else ""
        table = state.get("which", {})
        if binary in table:
            sys.stdout.write(table[binary] + "\n")
            return 0
        return 1

    if args[:3] == ["shell", "wm", "size"]:
        sys.stdout.write(state.get("wm_size", "Physical size: 1080x1920") + "\n")
        return 0

    if args[:3] == ["shell", "dumpsys", "meminfo"]:
        default = Path(__file__).resolve().parents[1].joinpath(
            "fixtures/meminfo-1.txt").read_text(encoding="utf-8")
        sys.stdout.write(state.get("dumpsys_meminfo", default))
        return 0

    if args[:3] == ["shell", "dumpsys", "cpuinfo"]:
        default = (
            "Load: 1.2 / 1.0 / 0.9\n"
            "CPU usage from 10s to 0s ago:\n"
            "  12.5% 4321/com.example.app: 8% user + 4.5% kernel\n"
            "  3% 100/system_server: 2% user + 1% kernel\n"
        )
        sys.stdout.write(state.get("dumpsys_cpuinfo", default))
        return 0

    if args[:3] == ["shell", "dumpsys", "gfxinfo"]:
        sys.stdout.write(state.get("dumpsys_gfxinfo", ""))
        return 0

    if args[:2] == ["shell", "cat"] and len(args) > 2 and args[2].startswith("/proc/"):
        default = "Threads:\t42\nVmRSS:\t8500 kB\nVmSize:\t120000 kB\n"
        sys.stdout.write(state.get("proc_status", default))
        return 0

    if args[:2] == ["shell", "pidof"]:
        package = args[-1]
        sys.stdout.write((state.get("pidof", {}).get(package, "")) + "\n")
        return 0

    if args[:4] == ["shell", "settings", "get", "global"]:
        value = state.get("settings", {}).get(args[4], "null")
        sys.stdout.write(f"{value}\n")
        return 0

    if args[:4] == ["shell", "settings", "put", "global"]:
        settings = state.setdefault("settings", {})
        settings[args[4]] = args[5]
        write_state(state)
        return 0

    # install / shell input / shell am / shell pm all succeed silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
