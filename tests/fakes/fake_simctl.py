#!/usr/bin/env python3
"""Deterministic stand-in for `xcrun` (simctl subset), used by Autonom tests.

Invoked exactly as the real driver is: ``<this> simctl <subcommand> …``.
Records every invocation to ``$AUTONOM_FAKE_LOG`` and reads canned state from
``$AUTONOM_FAKE_STATE``.

State keys used by the simulator controls (all optional):

``shutdown_ignored`` ``shutdown`` exits 0 but leaves the device state unchanged
``ui``               ``{"appearance": ..., "content_size": ...}`` that ``simctl ui``
                     reports and updates
``pasteboard``       what ``pbcopy`` stored and ``pbpaste`` prints
``status_bar``       the live overrides (flag name -> value) ``status_bar override``
                     stores, ``list`` prints and ``clear`` drops
``status_bar_ignored`` ``override``/``clear`` exit 0 and change nothing
``status_bar_names`` ``list`` prints enumeration names instead of raw values
``notify``           Darwin notification states ``notifyutil -s`` stores and
                     ``notifyutil -g`` prints
``notify_ignored``   ``notifyutil -s`` exits 0 and stores nothing
``app_info``         ``{bundle: {"CFBundleExecutable": ..., ...}}`` that ``appinfo``
                     prints; any other bundle is an error, as for an app that is
                     not installed
``app_bundle``       the path ``get_app_container <udid> <bundle> app`` prints
                     (default ``(null)``: no bundle a test did not create)
``privacy_help``     the text ``help privacy`` prints (default: Xcode 27.0's)
``simctl_hang``      ``{argv prefix: seconds}``: sleep that long first (a wedged
                     simctl, for timeout paths)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "00057dd8b7c40000000049454e44ae426082"
)

DEFAULT_DEVICES = {
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
            {
                "udid": "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB",
                "name": "iPhone 17 Pro",
                "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                "state": "Shutdown",
                "isAvailable": True,
            }
        ]
    }
}


# `xcrun simctl help privacy`, Xcode 27.0 (it prints to stderr and exits 0).
PRIVACY_HELP = """Grant, revoke, or reset privacy and permissions
Usage: simctl privacy <device> <action> <service> [<bundle identifier>]

\taction
\t     The action to take:
\t         grant - Grant access without prompting. Requires bundle identifier.
\t         revoke - Revoke access, denying all use of the service. Requires bundle identifier.
\t         reset - Reset access, prompting on next use. Bundle identifier optional.
\t     Some permission changes will terminate the application if running.
\tservice
\t     The service:
\t         all - Apply the action to all services.
\t         calendar - Allow access to calendar.
\t         contacts-limited - Allow access to basic contact info.
\t         contacts - Allow access to full contact details.
\t         location - Allow access to location services when app is in use.
\t         location-always - Allow access to location services at all times.
\t         photos-add - Allow adding photos to the photo library.
\t         photos - Allow full access to the photo library.
\t         media-library - Allow access to the media library.
\t         microphone - Allow access to audio input.
\t         motion - Allow access to motion and fitness data.
\t         reminders - Allow access to reminders.
\t         siri - Allow use of the app with Siri.
\tbundle identifier
\t     The bundle identifier of the target application.

Examples:
\treset all permissions: privacy <device> reset all
\tgrant test host photo permissions: privacy <device> grant photos com.example.app.test-host
"""

# The raw values Xcode 27.0's `status_bar override` parser assigns, which is
# what `status_bar list` prints back.
STATUS_BAR_ENUMS = {
    "dataNetwork": {"hide": 0, "wifi": 1, "3g": 6, "4g": 7, "lte": 8, "lte-a": 9,
                    "lte+": 10, "5g": 11, "5g+": 12, "5g-uwb": 13, "5g-uc": 14},
    "wifiMode": {"searching": 1, "failed": 2, "active": 3},
    "cellularMode": {"notSupported": 0, "searching": 1, "failed": 2, "active": 3},
    "batteryState": {"discharging": 0, "charging": 1, "charged": 2},
}


def status_bar_listing(overrides: dict, names: bool) -> str:
    """`status_bar list` as simctl prints it: a header, then one line per
    group that has an override (a group's other fields take simctl's
    defaults)."""
    def shown(key: str, default: str) -> str:
        value = overrides.get(key, default)
        if key in STATUS_BAR_ENUMS and not names:
            return str(STATUS_BAR_ENUMS[key].get(value, value))
        return str(value)

    lines = ["Current Status Bar Overrides:", "============================="]
    if "time" in overrides:
        lines.append(f"Time: {overrides['time']}")
    if "dataNetwork" in overrides:
        lines.append(f"DataNetworkType: {shown('dataNetwork', 'hide')}")
    if "wifiMode" in overrides or "wifiBars" in overrides:
        lines.append(f"WiFi Mode: {shown('wifiMode', 'active')}, "
                     f"WiFi Bars: {shown('wifiBars', '3')}")
    if {"cellularMode", "cellularBars", "operatorName"} & set(overrides):
        lines.append(f"Cell Mode: {shown('cellularMode', 'active')}, "
                     f"Cell Bars: {shown('cellularBars', '4')}")
        if "operatorName" in overrides:
            lines.append(f"Operator Name: {overrides['operatorName']}")
    if "batteryState" in overrides or "batteryLevel" in overrides:
        lines.append(f"Battery State: {shown('batteryState', 'charged')}, "
                     f"Battery Level: {shown('batteryLevel', '100')}, Not Charging: 0")
    return "\n".join(lines) + "\n"


def openstep(fields: dict) -> str:
    """`simctl appinfo`'s OpenStep-style dictionary."""
    body = []
    for key, value in sorted(fields.items()):
        text = str(value)
        if not text.replace("_", "").isalnum():
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        body.append(f"    {key} = {text};")
    return "{\n" + "\n".join(body) + "\n}\n"


def load_state() -> dict:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_state(state: dict) -> None:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if path:
        Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def record(argv: list[str]) -> None:
    path = os.environ.get("AUTONOM_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "simctl", "argv": argv}) + "\n")


def main(argv: list[str]) -> int:
    record(argv)
    state = load_state()

    for prefix, seconds in (state.get("simctl_hang") or {}).items():
        if " ".join(argv).startswith(prefix):
            time.sleep(float(seconds))

    for prefix, outcome in (state.get("simctl_fail") or {}).items():
        if " ".join(argv).startswith(prefix):
            code, message = outcome
            sys.stderr.write(message + "\n")
            return int(code)

    if argv[:1] == ["xctrace"]:
        # Off unless a test opts in — doctor/presets probes must stay honest
        # about hosts without Xcode.
        if not state.get("xctrace"):
            sys.stderr.write("xcrun: error: unable to find utility \"xctrace\"\n")
            return 1
        if argv[1:2] == ["version"]:
            sys.stdout.write("xctrace version 16.0\n")
            return 0
        if argv[1:2] == ["record"]:
            if state.get("xctrace_fail"):
                sys.stderr.write(state.get("xctrace_fail") + "\n")
                return 1
            output = argv[argv.index("--output") + 1]
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output) / "info.plist").write_text("fake trace\n")
            return 0
        return 0

    if argv[:1] != ["simctl"]:
        sys.stderr.write(f"fake xcrun: unsupported driver {argv[:1]}\n")
        return 1
    args = argv[1:]

    if args[:2] == ["list", "devices"]:
        sys.stdout.write(json.dumps(state.get("simctl_devices", DEFAULT_DEVICES)))
        return 0

    if args[:1] == ["bootstatus"]:
        devices = state.setdefault("simctl_devices", json.loads(json.dumps(DEFAULT_DEVICES)))
        for entries in devices["devices"].values():
            for entry in entries:
                if entry["udid"] == args[1]:
                    entry["state"] = "Booted"
        write_state(state)
        return 0

    if args[:1] == ["shutdown"]:
        # Mirror the real state machine so a shutdown-write-boot sequence can
        # be proven by the device list rather than assumed from exit codes.
        # `shutdown_ignored` reproduces a shutdown that returned 0 and left
        # the device Booted.
        if state.get("shutdown_ignored"):
            return 0
        devices = state.setdefault("simctl_devices", json.loads(json.dumps(DEFAULT_DEVICES)))
        for entries in devices["devices"].values():
            for entry in entries:
                if args[1:2] == ["all"] or entry["udid"] == args[1]:
                    entry["state"] = "Shutdown"
        write_state(state)
        return 0

    if args[:1] == ["ui"] and len(args) >= 3:
        # `ui <udid> appearance|content_size [value]`: set, or print the current.
        ui = state.setdefault("ui", {})
        defaults = {"appearance": "light", "content_size": "large"}
        if len(args) > 3:
            ui[args[2]] = args[3]
            write_state(state)
            return 0
        sys.stdout.write(str(ui.get(args[2], defaults.get(args[2], ""))) + "\n")
        return 0

    if args[:2] == ["help", "privacy"]:
        sys.stderr.write(state.get("privacy_help", PRIVACY_HELP))
        return 0

    if args[:1] == ["appinfo"] and len(args) >= 3:
        info = (state.get("app_info") or {}).get(args[2])
        if not info:
            sys.stderr.write("An error was encountered processing the command "
                             "(domain=IXErrorDomain, code=2):\nNo such app\n")
            return 1
        sys.stdout.write(openstep({"CFBundleIdentifier": args[2], **info}))
        return 0

    if args[:1] == ["status_bar"] and len(args) >= 3:
        overrides = state.setdefault("status_bar", {})
        if args[2] == "list":
            sys.stdout.write(status_bar_listing(overrides, bool(state.get("status_bar_names"))))
            return 0
        if args[2] == "clear":
            if not state.get("status_bar_ignored"):
                overrides.clear()
                write_state(state)
            return 0
        if args[2] == "override":
            flags = args[3:]
            if not flags:
                sys.stderr.write("No arguments were specified, nothing to do.\n")
                return 1
            if not state.get("status_bar_ignored"):
                for flag, value in zip(flags[::2], flags[1::2]):
                    overrides[flag.lstrip("-")] = value
                write_state(state)
            return 0
        sys.stderr.write(f"Unknown argument '{args[2]}'\n")
        return 1

    if args[:1] == ["spawn"] and args[2:3] == ["notifyutil"]:
        # `notifyutil -s <name> <state> -p <name> -g <name>`, in order.
        notify = state.setdefault("notify", {})
        rest = args[3:]
        index = 0
        while index < len(rest):
            option = rest[index]
            if option == "-s" and index + 2 < len(rest):
                if not state.get("notify_ignored"):
                    notify[rest[index + 1]] = int(rest[index + 2])
                    write_state(state)
                index += 3
            elif option == "-g" and index + 1 < len(rest):
                sys.stdout.write(f"{rest[index + 1]} {notify.get(rest[index + 1], 0)}\n")
                index += 2
            elif option == "-p" and index + 1 < len(rest):
                index += 2
            else:
                index += 1
        return 0

    if args[:1] == ["pbcopy"]:
        state["pasteboard"] = sys.stdin.read()
        write_state(state)
        return 0

    if args[:1] == ["pbpaste"]:
        sys.stdout.write(state.get("pasteboard", ""))
        return 0

    if args[:1] == ["listapps"]:
        installed = state.get("installed", ["com.example.app"])
        sys.stdout.write("{" + " ".join(f'"{app}" = {{}};' for app in installed) + "}")
        return 0

    if args[:1] == ["install"]:
        installed = state.setdefault("installed", [])
        bundle = state.get("install_bundle_id", "com.example.app")
        if bundle not in installed:
            installed.append(bundle)
        write_state(state)
        return 0

    if args[:1] == ["uninstall"]:
        installed = state.setdefault("installed", [])
        if args[2] in installed:
            installed.remove(args[2])
        write_state(state)
        return 0

    if args[:1] == ["launch"]:
        sys.stdout.write(f"{args[2]}: 4242\n")
        return 0

    if args[:1] == ["terminate"]:
        return 0 if args[2] in state.get("running", ["com.example.app"]) else 1

    if args[:2] == ["io", args[1]] and len(args) >= 3 and args[2] == "screenshot":
        Path(args[3]).write_bytes(PNG)
        return 0

    if args[:1] == ["get_app_container"]:
        if args[3:4] == ["app"]:
            # No bundle on disk unless a test provides one: never a path on
            # the host that a test did not create.
            sys.stdout.write(state.get("app_bundle", "(null)") + "\n")
            return 0
        sys.stdout.write(state.get("container", "/tmp/fake-container") + "\n")
        return 0

    if args[:1] == ["spawn"] and "launchctl" in args:
        # `launchctl list` inside the sim: UIKit apps carry their bundle id.
        # Default pid 1 exists on every host, so `ps -p` measurements work.
        pid = state.get("launchctl_pid", "1")
        bundles = state.get("running", ["com.example.app"])
        sys.stdout.write("PID\tStatus\tLabel\n")
        for bundle in bundles:
            sys.stdout.write(f"{pid}\t0\tUIKitApplication:{bundle}[0xd5f][77]\n")
        return 0

    if args[:1] == ["spawn"] and "log" in args:
        for line in state.get("ios_log", []):
            sys.stdout.write(line + "\n")
        return 0

    # openurl / privacy / location / addmedia / shutdown succeed silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
