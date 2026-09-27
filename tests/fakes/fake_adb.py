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
``boot_ids``        mapping of serial -> kernel boot_id (``null`` = unreadable);
                    a fixed default id otherwise
``run_as_refused``  text run-as prints instead of a listing (system / release app)
``ui_dump_incomplete`` number of truncated dumps to return before a complete one
``battery_level``   level ``dumpsys battery`` reports (``set level`` updates it)
``battery_overridden`` true while ``dumpsys battery set``/``unplug`` overrides are live
``battery_ac``      "true"/"false" for ``AC powered`` (``set ac`` updates it)
``battery_real_level`` the level ``dumpsys battery reset`` returns to (default 100)
``signal_profile``  last ``emu gsm signal-profile`` value; ``signal_rssi`` /
                    ``signal_ber`` the last ``emu gsm signal`` pair
``settings_system`` / ``settings_secure`` the other two settings namespaces
``night_mode``      what ``cmd uimode night`` reports (``yes``/``no``)
``clipboard``       the text ``cmd clipboard set text`` stored, after the device
                    shell has parsed the line
``clipboard_unsupported`` answer "No shell command implementation." (API 36)
"""
from __future__ import annotations

import json
import os
import shlex
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
        text = Path(dump).read_text(encoding="utf-8") if dump else ""
        # `ui_dump_incomplete`: N dumps come back truncated (a screen
        # mid-transition) before a complete one — what a real emulator did.
        pending = int(state.get("ui_dump_incomplete", 0) or 0)
        if pending > 0:
            state["ui_dump_incomplete"] = pending - 1
            write_state(state)
            sys.stdout.write(text[: len(text) // 2])
            return 0
        sys.stdout.write(text)
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
        # The battery service remembers every `set`, prints the "UPDATES
        # STOPPED" banner while an override is live, and `reset` returns it to
        # the real readings — so a pin, and what a clear restores, can be read
        # back the way a real emulator reports them.
        if args[3:4] == ["set"] and len(args) > 5:
            state["battery_overridden"] = True
            if args[4] == "level":
                state["battery_level"] = args[5]
            elif args[4] in ("ac", "usb"):
                state[f"battery_{args[4]}"] = "true" if args[5] == "1" else "false"
            write_state(state)
            return 0
        if args[3:4] == ["unplug"]:
            state.update(battery_overridden=True, battery_ac="false", battery_usb="false")
            write_state(state)
            return 0
        if args[3:4] == ["reset"]:
            state.update(battery_overridden=False,
                         battery_level=state.get("battery_real_level", "100"),
                         battery_ac="false", battery_usb="false")
            write_state(state)
            return 0
        banner = ("Current Battery Service state:\n  (UPDATES STOPPED -- use 'reset' "
                  "to restart)\n" if state.get("battery_overridden")
                  else "Current Battery Service state:\n")
        sys.stdout.write(
            f"{banner}  AC powered: {state.get('battery_ac', 'false')}\n"
            f"  USB powered: {state.get('battery_usb', 'false')}\n"
            f"  status: 4\n  level: {state.get('battery_level', '100')}\n  scale: 100\n")
        return 0

    if args[:3] == ["emu", "gsm", "signal-profile"]:
        # The console accepts 0..4 and says OK; anything else is a KO.
        if len(args) < 4 or args[3] not in ("0", "1", "2", "3", "4"):
            sys.stdout.write("KO: bad signal profile\n")
            return 1
        state["signal_profile"] = args[3]
        write_state(state)
        sys.stdout.write("OK\n")
        return 0

    if args[:3] == ["emu", "gsm", "signal"]:
        # `gsm signal <rssi> [<ber>]`: rssi 0..31 or 99, ber 0..7 or 99.
        rssi = args[3] if len(args) > 3 else ""
        ber = args[4] if len(args) > 4 else "99"
        valid_rssi = rssi.isdigit() and (int(rssi) <= 31 or int(rssi) == 99)
        valid_ber = ber.isdigit() and (int(ber) <= 7 or int(ber) == 99)
        if not (valid_rssi and valid_ber):
            sys.stdout.write("KO: bad rssi or ber\n")
            return 1
        state.update(signal_rssi=rssi, signal_ber=ber)
        write_state(state)
        sys.stdout.write("OK\n")
        return 0

    if args[:1] == ["shell"] and len(args) > 1:
        # The device shell parses the joined line exactly as `sh` would, so a
        # badly quoted argument shows up here as the wrong words.
        try:
            words = shlex.split(" ".join(args[1:]))
        except ValueError:  # unbalanced quotes: sh would refuse the line too
            words = []
        if words[:3] == ["cmd", "clipboard", "set"]:
            if state.get("clipboard_unsupported"):
                sys.stdout.write("No shell command implementation.\n")
                return 0
            state["clipboard"] = " ".join(words[4:]) if words[3:4] == ["text"] else ""
            state["clipboard_words"] = len(words) - 4
            write_state(state)
            return 0
        if words[:3] == ["cmd", "uimode", "night"]:
            if len(words) > 3:
                state["night_mode"] = words[3]
                write_state(state)
            sys.stdout.write(f"Night mode: {state.get('night_mode', 'no')}\n")
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

    if args[:4] == ["shell", "cmd", "package", "resolve-activity"]:
        # `--brief` prints a priority line and then the component; a package
        # with no launcher activity prints "No activity found".
        package = args[-1]
        if package in (state.get("no_launcher") or []):
            sys.stdout.write("No activity found\n")
            return 0
        sys.stdout.write(f"priority=0 preferredOrder=0 match=0x108000\n{package}/.MainActivity\n")
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

    if args[:3] == ["shell", "cat", "/proc/sys/kernel/random/boot_id"]:
        # A fresh random id per boot; tests model "another emulator on the
        # same serial" by changing it (and ``avd_names``) under the serial.
        serial = argv[1] if argv[:1] == ["-s"] and len(argv) >= 2 else ""
        boot_ids = state.get("boot_ids") or {}
        if serial in boot_ids and boot_ids[serial] is None:
            sys.stdout.write("cat: /proc/sys/kernel/random/boot_id: Permission denied\n")
            return 1
        sys.stdout.write(boot_ids.get(serial, "5f3c2a10-0000-4000-8000-000000000001") + "\n")
        return 0

    if args[:2] == ["shell", "cat"] and len(args) > 2 and args[2].startswith("/proc/"):
        default = "Threads:\t42\nVmRSS:\t8500 kB\nVmSize:\t120000 kB\n"
        sys.stdout.write(state.get("proc_status", default))
        return 0

    if args[:2] == ["shell", "pidof"]:
        package = args[-1]
        sys.stdout.write((state.get("pidof", {}).get(package, "")) + "\n")
        return 0

    if args[:2] == ["shell", "settings"] and len(args) > 4 and args[3] in (
            "global", "system", "secure"):
        # `settings` keeps its original global table under `settings`.
        table = "settings" if args[3] == "global" else f"settings_{args[3]}"
        if args[2] == "get":
            value = state.get(table, {}).get(args[4], "null")
            sys.stdout.write(f"{value}\n")
            return 0
        if args[2] == "put" and len(args) > 5:
            state.setdefault(table, {})[args[4]] = args[5]
            write_state(state)
            return 0
        if args[2] == "delete":
            state.setdefault(table, {}).pop(args[4], None)
            write_state(state)
            sys.stdout.write("Deleted 1 rows\n")
            return 0

    # install / shell input / shell am / shell pm all succeed silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
