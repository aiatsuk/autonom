#!/usr/bin/env python3
"""Stand-ins for `javac`, `d8` and `java`, used by the helper build tests.

Tests install small shell wrappers named after the tools that run this file
with the tool's name as the first argument (``fake_android_tools.py d8
--version``); the wrapper exports ``AUTONOM_FAKE_TOOL_PATH`` (its own path),
so the log shows which copy ran.

Every call appends one JSON line to ``$AUTONOM_FAKE_TOOLS_LOG``:
``{"tool", "path", "argv", "path0", "java"}`` — ``path0`` is the first PATH
entry and ``java`` what ``java`` resolves to on PATH, which is how a test
proves the build put the JDK's ``bin`` first for the SDK's ``d8`` script.

Behaviour, all optional:

``AUTONOM_FAKE_JAVAC_VERSION``  text of ``javac -version`` (default "javac 21.0.0-fake")
``AUTONOM_FAKE_D8_VERSION``     text of ``d8 --version`` (default "D8 8.0.0-fake")
``AUTONOM_FAKE_VERSION_FAIL``   "javac" or "d8": that tool's version probe exits 1
``AUTONOM_FAKE_JAVAC_FAIL``     any value: compiling exits 1 with a compiler error
``AUTONOM_FAKE_D8_FAIL``        any value: dexing exits 1
``AUTONOM_FAKE_D8_NO_OUTPUT``   any value: dexing exits 0 and writes nothing
``AUTONOM_FAKE_TOOL_DELAY_S``   seconds a compile or dex step sleeps before it writes

Compiling writes one ``<Name>.class`` per source under ``-d`` (in the
directory of the source's ``package``); dexing needs an existing
``--output`` directory, as the real d8 does, and writes ``classes.dex``
starting with the dex magic.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

TOOLS = ("javac", "d8", "java")


def log(tool: str, argv: list[str]) -> None:
    target = os.environ.get("AUTONOM_FAKE_TOOLS_LOG")
    if not target:
        return
    path = os.environ.get("PATH", "")
    entry = {
        "tool": tool,
        "path": os.environ.get("AUTONOM_FAKE_TOOL_PATH"),
        "argv": argv,
        "path0": path.split(os.pathsep)[0] if path else "",
        "java": shutil.which("java"),
    }
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def delay() -> None:
    seconds = float(os.environ.get("AUTONOM_FAKE_TOOL_DELAY_S") or 0)
    if seconds > 0:
        time.sleep(seconds)


def javac(argv: list[str]) -> int:
    if argv == ["-version"]:
        if os.environ.get("AUTONOM_FAKE_VERSION_FAIL") == "javac":
            print("javac: broken runtime", file=sys.stderr)
            return 1
        print(os.environ.get("AUTONOM_FAKE_JAVAC_VERSION") or "javac 21.0.0-fake")
        return 0
    if "-d" not in argv:
        print("javac: no -d", file=sys.stderr)
        return 2
    out = Path(argv[argv.index("-d") + 1])
    delay()
    if os.environ.get("AUTONOM_FAKE_JAVAC_FAIL"):
        print("Main.java:1: error: fake compile failure", file=sys.stderr)
        print("1 error", file=sys.stderr)
        return 1
    for source in (Path(arg) for arg in argv if arg.endswith(".java")):
        text = source.read_text(encoding="utf-8")
        package = re.search(r"^package\s+([\w.]+);", text, re.M)
        folder = out.joinpath(*package.group(1).split(".")) if package else out
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode()).digest()
        (folder / (source.stem + ".class")).write_bytes(b"\xca\xfe\xba\xbe" + digest)
    return 0


def d8(argv: list[str]) -> int:
    if argv == ["--version"]:
        if os.environ.get("AUTONOM_FAKE_VERSION_FAIL") == "d8":
            print("Error: could not find java", file=sys.stderr)
            return 1
        print(os.environ.get("AUTONOM_FAKE_D8_VERSION") or "D8 8.0.0-fake")
        return 0
    if "--output" not in argv:
        print("d8: no --output", file=sys.stderr)
        return 2
    out = Path(argv[argv.index("--output") + 1])
    delay()
    if os.environ.get("AUTONOM_FAKE_D8_FAIL"):
        print("Error: fake dex failure", file=sys.stderr)
        return 1
    if not out.is_dir():
        print(f"Invalid output: {out}\nOutput must be a .zip or .jar archive or an existing directory",
              file=sys.stderr)
        return 1
    if os.environ.get("AUTONOM_FAKE_D8_NO_OUTPUT"):
        return 0
    digest = hashlib.sha256()
    for arg in argv:
        if arg.endswith(".class"):
            digest.update(Path(arg).read_bytes())
    (out / "classes.dex").write_bytes(b"dex\n035\0" + digest.digest())
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in TOOLS:
        tool, rest = argv[0], argv[1:]
    else:
        tool, rest = Path(sys.argv[0]).name, argv
    log(tool, rest)
    if tool == "javac":
        return javac(rest)
    if tool == "d8":
        return d8(rest)
    if tool == "java":
        print('openjdk version "21.0.0-fake"', file=sys.stderr)
        return 0
    print(f"fake_android_tools: unknown tool {tool}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
