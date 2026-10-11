#!/usr/bin/env python3
"""Stand-in for ``scripts/autonom.py`` used by the ChatGPT probe server tests.

It answers only the two commands the probe runs, ``version`` and
``devices list``. ``$FAKE_PROBE_CLI_MODE`` picks the behaviour:

``ok``       (default) canned JSON for both commands
``slow``     sleep 30 s before answering (the server must kill it)
``error``    print an Autonom ``ok: false`` error and exit 1
``garbage``  print text that is not JSON and exit 0
``no_version`` / ``no_devices``  valid JSON without the ``version`` / ``devices`` key

``$FAKE_PROBE_CLI_LOG`` (optional) gets one JSON line per call:
``{"pid": N, "argv": [...]}``, written before any sleep, so a test can check
the exact argument array and that a slow call's process is gone.
"""
from __future__ import annotations

import json
import os
import sys
import time


def main(argv: list) -> int:
    log = os.environ.get("FAKE_PROBE_CLI_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "argv": argv}) + "\n")
    mode = os.environ.get("FAKE_PROBE_CLI_MODE", "ok")
    if mode == "slow":
        time.sleep(30)
    if mode == "garbage":
        print("this is not JSON")
        return 0
    if mode == "error":
        print(json.dumps({"ok": False, "error_code": "adb_not_found", "error": "adb was not found",
                          "hint": "Install Android platform-tools."}, indent=2))
        return 1
    if argv == ["version"] and mode == "no_version":
        print(json.dumps({"name": "autonom"}))
        return 0
    if argv == ["devices", "list"] and mode == "no_devices":
        print(json.dumps({"ok": True, "warnings": []}))
        return 0
    if argv == ["version"]:
        print(json.dumps({"name": "autonom", "version": "0.35.1"}, indent=2))
        return 0
    if argv == ["devices", "list"]:
        print(json.dumps({
            "ok": True,
            "devices": [
                {"platform": "android", "target_id": "emulator-5584", "state": "device",
                 "name": "sdk_gphone64_arm64", "avd": "Autonom_Split_API36", "serial_extra": "dropped"},
                {"platform": "ios", "target_id": "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5",
                 "state": "booted", "name": "Autonom-Split-Test", "runtime": "dropped"},
            ],
            "warnings": [{"code": "emulator_missing", "error": "x", "hint": "y"}],
        }, indent=2))
        return 0
    print(json.dumps({"ok": False, "error_code": "unknown_command", "error": "fake CLI: %r" % argv,
                      "hint": "only version and devices list"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
