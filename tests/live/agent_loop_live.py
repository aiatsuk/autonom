#!/usr/bin/env python3
"""Live check of the agent loop on one bench device (VER-003 iOS, VER-004 Android).

Not part of the unit suite. It drives the Settings app of the one explicit
device and must only run on a private test device no other session uses:

    python3 tests/live/agent_loop_live.py --platform ios --udid <UDID> --evidence-dir DIR
    python3 tests/live/agent_loop_live.py --platform android --serial <SERIAL> --evidence-dir DIR

The device must already be booted; the script never boots or shuts one down.
Every run uses its own temporary AUTONOM_HOME (removed at the end unless
--keep-home), so it never reads, stops or writes the user's sessions, and
starts one session there with `session start --profile agent`. Every check
is decided from what the CLI replied and from a read the script makes on its
own, never from the CLI alone.

iOS (VER-003), Settings (com.apple.Preferences), session on the idb route.
idb and AXe are reached through wrappers in a temporary directory put first
on PATH: each logs its argv (one JSON line) and then runs the real tool, so
the log counts every read and every input of each CLI call. The independent
read is `axe describe-ui` with the real AXe.

- observe, single_read: `ui tap --desc General` from the main screen returns
  `screen.outline` with About and `foreground.app` Settings; the idb log of
  that call has exactly one `ui describe-all` before its one `ui tap`.
- quiet_settle: `ui tap --desc About --no-observe`, then `ui wait --settled`
  answers `method: idb-quiet`, `snapshots: 1`, with one `ui quiet` and one
  `ui describe-all` in its log.
- axe_tap: Settings relaunched to its main screen; `--ios-hid axe ui tap
  --desc General` sends exactly one `axe touch ... --down --up --delay 0.12`
  (no idb tap, one `describe-all` before it), and a fresh read shows the
  General page.
- dead_companion: the one live local idb_companion idb's registry
  (/tmp/idb/state, or AUTONOM_IDB_STATE_FILE) lists for this UDID, after its
  command line is checked to name idb_companion and this UDID, is killed
  (SIGKILL); one `ui tree` must then succeed. Nothing else is killed; when
  the registry does not list exactly one such companion, or under 60 s of
  the time limit are left, nothing is killed and the check fails. Companions
  of other simulators must be alive after it exactly as before.
- ui_do: Settings relaunched; the six-step walk-through (General, About,
  back, back, Accessibility, back) as one `ui do` passes every step with one
  idb tap per step, and a fresh read shows the main Settings screen.
- one_line: every stdout line of every CLI call is one JSON object.
- scope: every idb or AXe call that names a UDID names this one.

Android (VER-004), Settings (com.android.settings). The independent read is
`adb exec-out uiautomator dump /dev/tty`; `settings get global
airplane_mode_on` must read 0 before and after (nothing toggles it).

- observe, outline_state: `ui tap --text "Network & internet"` returns
  `screen.outline` and `foreground.app` com.android.settings; in the
  outline the switch of the Airplane mode row (a line of its own, beside the
  title) is marked exactly `unchecked`.
- not_idle: the About page (`am start -a
  android.settings.DEVICE_INFO_SETTINGS`), then `ui tree` exits 2 with
  `ui_not_idle`.
- ui_do: Settings restarted on its main screen; the six-step walk-through
  (Network & internet, back, Display & touch, back, Notifications, back) as
  one `ui do` passes every step, and a fresh read shows the main list.
- airplane_mode, one_line: as above.

Writes <evidence-dir>/agent-loop-ios.json or agent-loop-android.json with
every check (`ok` and what decided it), every CLI call (argv, exit code,
seconds, the idb/AXe calls it made) and the host load at start and end, and
prints one JSON line naming it. Exit 0 when every check passed, 1 otherwise,
2 for a usage error. `--help` and usage errors touch no device.

The whole run, cleanup included, ends within --time-limit (default 1500 s,
under the cases' 1800 s limit): every step's own timeout is cut to what is
left, the last 120 s are kept for the cleanup, and a step with no time left
is not started and fails its check. SIGTERM or SIGHUP (a runner's timeout)
stops the steps too: the report is written at once with `terminated`, then
the session is stopped and the temporary home removed, and the exit is
128 + the signal number.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "autonom.py"

IOS_APP = "com.apple.Preferences"
ANDROID_APP = "com.android.settings"
ANDROID_MAIN = "com.android.settings/.Settings"
ANDROID_ABOUT_ACTION = "android.settings.DEVICE_INFO_SETTINGS"
IDB_STATE_DEFAULT = "/tmp/idb/state"
AXE_HOLD = "0.12"
CLI_TIMEOUT = 180
DO_TIMEOUT = 600
ROOT_WAIT_S = 30
# The whole run ends within this, cleanup included: VER-003 and VER-004 give
# a run 1800 s, and a runner that times out kills the script before any
# cleanup of its own could run.
TIME_LIMIT_S = 1500
# Kept back from the steps for the cleanup: `session stop` (STOP_TIMEOUT)
# and, on Android, the last airplane-mode read (FINAL_READ_TIMEOUT).
CLEANUP_RESERVE_S = 120
STOP_TIMEOUT = 60
FINAL_READ_TIMEOUT = 30
# Every check a passing run must have decided, per platform.
EXPECTED_CHECKS = {
    "ios": ("session", "observe", "single_read", "quiet_settle", "axe_tap", "dead_companion",
            "ui_do", "scope", "one_line"),
    "android": ("session", "observe", "outline_state", "not_idle", "ui_do", "airplane_mode",
                "one_line"),
}

# The Settings walk-throughs of the live A/B run (REPORT.md, ios-nav.yaml and
# android-nav.yaml), as `ui do` takes them: a JSON array of Flow v1 steps.
IOS_WALK: list[Any] = [
    {"tapOn": {"visibleText": "General"}},
    {"tapOn": {"visibleText": "About"}},
    {"tapOn": {"id": "BackButton"}},
    {"tapOn": {"id": "BackButton"}},
    {"tapOn": {"visibleText": "Accessibility"}},
    {"tapOn": {"id": "BackButton"}},
]
ANDROID_WALK: list[Any] = [
    {"tapOn": {"text": "Network & internet"}},
    "back",
    {"tapOn": {"text": "Display & touch"}},
    "back",
    {"tapOn": {"text": "Notifications"}},
    "back",
]

# Which screen an independent read shows, from the labels on it (the A/B
# bench's rules).
IOS_SCREENS: dict[str, Callable[[set[str]], bool]] = {
    "root": lambda labels: ({"General", "Accessibility", "Appearance"} <= labels
                            and "About" not in labels),
    "general": lambda labels: {"About", "Language & Region"} <= labels,
    "about": lambda labels: any(label.startswith("iOS Version") for label in labels),
}
ANDROID_SCREENS: dict[str, Callable[[set[str]], bool]] = {
    "root": lambda labels: {"Network & internet", "Connected devices"} <= labels,
    "network": lambda labels: "Airplane mode" in labels,
}

# Overrides of the caller's environment that would point the CLI at other
# binaries, another backend, another companion or a control claim.
SCRUBBED_ENV = (
    "AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_EMULATOR", "AUTONOM_AXE",
    "AUTONOM_IOS_HID", "AUTONOM_UI_BACKEND", "AUTONOM_IDB_COMPANION",
    "AUTONOM_CONTROL_HOLDER", "AUTONOM_CONTROL_GENERATION",
)


class StepFailed(Exception):
    pass


class OutOfTime(StepFailed):
    """The run's time limit leaves no time for a step; it was not started."""


class Terminated(BaseException):
    """SIGTERM or SIGHUP: raised so that main's cleanup still runs (the
    default action of both ends the process without it)."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"stopped by {signal.Signals(signum).name}")
        self.signum = signum


class Budget:
    """The run's time limit: each step's timeout is cut to what is left of it
    before the cleanup reserve; after `end_steps` (the cleanup) nothing is cut
    and the cleanup calls use their own short timeouts."""

    def __init__(self) -> None:
        self.steps_until: float | None = None
        self.exhausted = False

    def start(self, limit_s: float) -> None:
        self.steps_until = time.monotonic() + limit_s - CLEANUP_RESERVE_S
        self.exhausted = False

    def end_steps(self) -> None:
        self.steps_until = None

    def clamp(self, timeout: float) -> float:
        if self.steps_until is None:
            return timeout
        left = self.steps_until - time.monotonic()
        if left < 1:
            self.exhausted = True
            raise OutOfTime("the run's time limit leaves no time for this step; "
                            "it was not started")
        return min(timeout, left)

    def require(self, seconds: float, what: str) -> None:
        """OutOfTime unless ``seconds`` are left for ``what``."""
        if self.steps_until is not None and self.steps_until - time.monotonic() < seconds:
            self.exhausted = True
            raise OutOfTime(f"the run's time limit leaves under {seconds:g} s for {what}; "
                            "it was not started")


BUDGET = Budget()
# Left at least for killing the companion and the `ui tree` that must recover it.
KILL_NEEDS_S = 60


def _raise_terminated(signum: int, _frame: Any) -> None:
    raise Terminated(signum)


def _note_late_signal(signum: int, _frame: Any) -> None:
    """During the cleanup a second signal does not cut it short (a runner
    follows up with SIGKILL anyway). A handler, not SIG_IGN: an ignored
    signal would stay ignored in every child the cleanup starts."""


def run(argv: list[str], *, timeout: float, env: dict[str, str] | None = None,
        input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    timeout = BUDGET.clamp(timeout)
    extra: dict[str, Any] = ({"input": input_text} if input_text is not None
                             else {"stdin": subprocess.DEVNULL})
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               env=env, check=False, **extra)
    if check and completed.returncode != 0:
        raise StepFailed(f"{' '.join(argv[:5])} failed ({completed.returncode}): "
                         f"{(completed.stderr or completed.stdout).strip()[-300:]}")
    return completed


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host_load() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _json_object(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def one_line_ok(stdout: str, stderr: str, code: int | None) -> bool:
    """A reply on stdout is exactly one line holding one JSON object; with
    nothing on stdout (an error) the stderr envelope ends in one."""
    out = [line for line in stdout.splitlines() if line.strip()]
    if out:
        return len(out) == 1 and _json_object(out[0])
    err = [line for line in stderr.splitlines() if line.strip()]
    return code not in (0, None) and bool(err) and _json_object(err[-1])


def parse_reply(stdout: str, stderr: str) -> dict[str, Any]:
    for stream in (stdout, stderr):
        text = stream.strip()
        if not text:
            continue
        for candidate in (text, text.splitlines()[-1]):
            try:
                value = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {"ok": False, "error": (stderr or stdout).strip()[-300:]}


# --- the idb/AXe argv log ----------------------------------------------------------


def write_wrapper(directory: Path, name: str, real: str, log: Path) -> Path:
    """`name` on PATH that appends its argv to ``log``, then runs ``real``."""
    path = directory / name
    path.write_text(
        f"#!{sys.executable} -I\n"
        f"# Logs every {name} call of the live agent-loop check, then runs the real {name}.\n"
        "import json, os, sys, time\n"
        f"LOG = {str(log)!r}\n"
        f"REAL = {real!r}\n"
        "with open(LOG, 'a', encoding='utf-8') as handle:\n"
        f"    handle.write(json.dumps({{'tool': {name!r}, 't': round(time.time(), 3), "
        "'argv': sys.argv[1:]}) + '\\n')\n"
        "os.execv(REAL, [REAL, *sys.argv[1:]])\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


class ToolLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.touch()

    def entries(self) -> list[dict[str, Any]]:
        found = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("argv"), list):
                found.append(entry)
        return found

    def mark(self) -> int:
        return len(self.entries())

    def since(self, mark: int) -> list[dict[str, Any]]:
        return self.entries()[mark:]


def idb_command(argv: list[str]) -> str:
    """`ui describe-all`, `ui tap`, `list-targets`...: the verb after idb's
    top-level options (`--companion HOST:PORT`, `--log LEVEL`)."""
    args = list(argv)
    while args and args[0].startswith("--"):
        args = args[2:]
    if not args:
        return ""
    return " ".join(args[:2]) if args[0] in ("ui", "crash") and len(args) > 1 else args[0]


def tool_command(entry: dict[str, Any]) -> str:
    argv = [str(item) for item in entry.get("argv") or []]
    if entry.get("tool") == "idb":
        return idb_command(argv)
    return argv[0] if argv else ""


def option_value(argv: list[str], option: str) -> str | None:
    for index, item in enumerate(argv[:-1]):
        if item == option:
            return argv[index + 1]
    return None


def count(entries: list[dict[str, Any]], tool: str, command: str) -> int:
    return sum(1 for entry in entries
               if entry.get("tool") == tool and tool_command(entry) == command)


def summary(entries: list[dict[str, Any]]) -> list[str]:
    return [f"{entry.get('tool')} {tool_command(entry)}" for entry in entries]


# --- the CLI under test ------------------------------------------------------------


class Reply:
    def __init__(self, code: int | None, payload: dict[str, Any],
                 tools: list[dict[str, Any]]) -> None:
        self.code = code
        self.payload = payload
        self.tools = tools

    @property
    def ok(self) -> bool:
        return self.payload.get("ok") is True


class Cli:
    """`python3 scripts/autonom.py <global flags> <verb> ... <target flag>` in the
    temporary home; every call is recorded for the evidence."""

    def __init__(self, target: list[str], env: dict[str, str], log: ToolLog | None) -> None:
        self.target = target
        self.env = env
        self.log = log
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *argv: str, pre: tuple[str, ...] = (),
                 stdin: str | None = None, timeout: float = CLI_TIMEOUT) -> Reply:
        command = [sys.executable, str(CLI), *pre, *argv, *self.target]
        timeout = BUDGET.clamp(timeout)  # OutOfTime: the call is not made
        mark = self.log.mark() if self.log else 0
        started = time.monotonic()
        try:
            completed = run(command, timeout=timeout, env=self.env, input_text=stdin,
                            check=False)
            stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, code = _text(exc.stdout), _text(exc.stderr), None
        seconds = round(time.monotonic() - started, 2)
        tools = self.log.since(mark) if self.log else []
        payload = parse_reply(stdout, stderr)
        if code is None:
            payload = {"ok": False, "error": f"no reply within {timeout:g} s"}
        self.calls.append({
            "name": name, "argv": [*pre, *argv, *self.target], "exit": code,
            "seconds": seconds, "ok": payload.get("ok"),
            "error_code": payload.get("error_code"),
            "stdout_lines": len([line for line in stdout.splitlines() if line.strip()]),
            "one_line": one_line_ok(stdout, stderr, code),
            "tool_calls": summary(tools),
        })
        return Reply(code, payload, tools)


def outline_lines(outline: str) -> list[tuple[int, str, str | None, list[str]]]:
    """(indent, line, label, flags) of each outline line."""
    parsed = []
    for line in outline.splitlines():
        if not line.strip():
            continue
        indent = (len(line) - len(line.lstrip(" "))) // 2
        label = None
        match = re.search(r'"((?:[^"\\]|\\.)*)"', line)
        if match:
            try:
                label = json.loads(f'"{match.group(1)}"')
            except ValueError:
                label = match.group(1)
        flags_match = re.search(r"\[([a-z ]+)\]\s*$", line)
        flags = flags_match.group(1).split() if flags_match else []
        parsed.append((indent, line.strip(), label, flags))
    return parsed


def outline_labels(outline: str) -> set[str]:
    return {label for _, _, label, _ in outline_lines(outline) if label}


CHECK_STATES = ("checked", "unchecked")
_OUTLINE_BOUNDS = re.compile(r"@(-?\d+),(-?\d+) (\d+)x(\d+)")


def outline_nodes(outline: str) -> list[dict[str, Any]]:
    """Each outline line as {indent, text, label, flags, role, span}; span is
    the line's vertical extent (top, bottom) from its `@x,y wxh`, or None."""
    nodes = []
    for indent, text, label, flags in outline_lines(outline):
        bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text, count=1)  # no bounds read from a label
        tokens = bare.split()
        bounds = _OUTLINE_BOUNDS.search(bare)
        span = None
        if bounds:
            top = int(bounds.group(2))
            span = (top, top + int(bounds.group(4)))
        nodes.append({"indent": indent, "text": text, "label": label, "flags": flags,
                      "role": tokens[1].lower() if len(tokens) > 1 else "", "span": span})
    return nodes


def is_checkable_line(node: dict[str, Any]) -> bool:
    """A switch, check box or radio line, or any line showing a check state."""
    role = node["role"]
    return (any(flag in CHECK_STATES for flag in node["flags"])
            or "switch" in role or role in ("checkbox", "radio"))


def spans_overlap(first: tuple[int, int] | None, second: tuple[int, int] | None) -> bool:
    return (first is not None and second is not None
            and first[0] < second[1] and second[0] < first[1])


def row_switches(outline: str, label: str) -> list[dict[str, Any]]:
    """The switch of the row of each line labelled ``label``.

    On Android the check state of a Settings row belongs to its Switch, a
    line of its own beside the title, not to the title or its icon:

        n27 linearlayout @0,1014 1080x155 [clickable]
              n31 text "Airplane mode" id=android:id/title @189,1056 342x71
            n32 linearlayout id=android:id/widget_frame @859,1014 179x155
              n33 switch id=com.android.settings:id/switchWidget @901,1028 137x126 [unchecked]

    A title line that is itself checkable is its own switch ("title").
    Otherwise its row is its nearest clickable ancestor in the outline
    indentation (or the title itself when clickable), and the row's switches
    are the checkable lines under that row whose vertical extent overlaps the
    title's (or is not known) ("row"). When the row has none, the nearest
    checkable line whose vertical extent overlaps the title's is taken
    ("overlap"). One entry per labelled line: {title, row, how, switches
    (lines), switch_states (the check states each switch shows)}."""
    nodes = outline_nodes(outline)
    rows: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if node["label"] != label:
            continue
        row = None
        picked: list[int] = []
        how = None
        if is_checkable_line(node):
            picked, how = [index], "title"
        else:
            if "clickable" in node["flags"]:
                row = index
            else:
                depth = node["indent"]
                for back in range(index - 1, -1, -1):
                    if nodes[back]["indent"] >= depth:
                        continue
                    depth = nodes[back]["indent"]  # the next ancestor up
                    if "clickable" in nodes[back]["flags"]:
                        row = back
                        break
            if row is not None:
                for later in range(row + 1, len(nodes)):
                    if nodes[later]["indent"] <= nodes[row]["indent"]:
                        break
                    candidate = nodes[later]
                    if (later != index and is_checkable_line(candidate)
                            and (candidate["span"] is None or node["span"] is None
                                 or spans_overlap(candidate["span"], node["span"]))):
                        picked.append(later)
                if picked:
                    how = "row"
            if not picked:
                near = [other for other, candidate in enumerate(nodes)
                        if other != index and is_checkable_line(candidate)
                        and spans_overlap(candidate["span"], node["span"])]
                if near:
                    picked, how = [min(near, key=lambda other: abs(other - index))], "overlap"
        rows.append({
            "title": node["text"],
            "row": nodes[row]["text"] if row is not None else None,
            "how": how,
            "switches": [nodes[other]["text"] for other in picked],
            "switch_states": [[flag for flag in nodes[other]["flags"] if flag in CHECK_STATES]
                              for other in picked],
        })
    return rows


def outline_state_result(outline: str, label: str) -> dict[str, Any]:
    """`outline_state`: every row titled ``label`` has a switch, and every such
    switch shows exactly `unchecked` (fails on `checked`, on no state, and when
    a row has no switch)."""
    rows = row_switches(outline, label)
    states = [state for row in rows for found in row["switch_states"] for state in found]
    lines = [line for row in rows for line in (row["title"], *row["switches"])]
    ok = (bool(rows) and all(row["switches"] for row in rows)
          and all(found == ["unchecked"] for row in rows for found in row["switch_states"]))
    return {"rows": rows, "states": states, "lines": lines, "ok": ok}


def change_counts(payload: dict[str, Any]) -> dict[str, Any] | None:
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return None
    return {key: changes.get(key) for key in ("added", "removed", "changed")}


def warning_codes(payload: dict[str, Any]) -> list[str]:
    return [str(item.get("code")) for item in payload.get("warnings") or []
            if isinstance(item, dict)]


def step_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{key: step.get(key) for key in ("step", "command", "status", "duration_ms",
                                             "error_code") if key in step}
            for step in payload.get("steps") or [] if isinstance(step, dict)]


def guarded(report: dict[str, Any], names: tuple[str, ...],
            body: Callable[[], dict[str, dict[str, Any]]]) -> None:
    """Run ``body`` (it returns one result per check name); a failure fails
    every check it was to decide, and the next checks still run."""
    started = time.monotonic()
    try:
        results = body()
    except Exception as exc:  # noqa: BLE001 - live evidence records every failure
        error = f"{type(exc).__name__}: {exc}"[-400:]
        results = {name: {"ok": False, "error": error} for name in names}
    seconds = round(time.monotonic() - started, 1)
    for name in names:
        result = results.get(name) or {"ok": False, "error": "not decided"}
        result["ok"] = bool(result.get("ok"))
        result.setdefault("seconds", seconds)
        report["checks"][name] = result


def session_check(started: Reply) -> dict[str, Any]:
    """`session start` replies the new session's record under `session`
    (`{"ok": true, "session": {..., "profile": "agent"}, ...}`); the check
    needs exit 0 and that record's profile `agent`."""
    session = started.payload.get("session")
    record = session if isinstance(session, dict) else {}
    result = {"exit": started.code, "session_id": record.get("session_id"),
              "profile": record.get("profile"),
              "error_code": started.payload.get("error_code")}
    result["ok"] = started.code == 0 and started.ok and result["profile"] == "agent"
    return result


def one_line_check(cli: Cli) -> dict[str, Any]:
    broken = [call["name"] for call in cli.calls if not call["one_line"]]
    return {"calls": len(cli.calls), "not_one_line": broken,
            "ok": bool(cli.calls) and not broken}


def base_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in SCRUBBED_ENV:
        env.pop(name, None)
    env["AUTONOM_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# --- iOS (VER-003) -----------------------------------------------------------------


def axe_labels(text: str) -> set[str]:
    """Every AXLabel in `axe describe-ui` output."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "AXLabel" and isinstance(item, str) and item:
                    found.add(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    try:
        walk(json.loads(text))
    except ValueError:
        for raw in re.findall(r'"AXLabel"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
            try:
                found.add(json.loads(f'"{raw}"'))
            except ValueError:
                found.add(raw)
    return found


class Ios:
    def __init__(self, udid: str, axe: str, state_file: Path) -> None:
        self.udid = udid
        self.axe = axe
        self.state_file = state_file

    def labels(self) -> set[str]:
        return axe_labels(run([self.axe, "describe-ui", "--udid", self.udid],
                              timeout=120).stdout)

    def shows(self, screen: str) -> bool:
        return IOS_SCREENS[screen](self.labels())

    def state(self) -> str:
        listing = json.loads(run(["xcrun", "simctl", "list", "devices", "--json"],
                                 timeout=60).stdout)
        for devices in listing.get("devices", {}).values():
            for device in devices:
                if device.get("udid") == self.udid:
                    return str(device.get("state") or "")
        raise StepFailed(f"no simulator {self.udid}")

    def to_root(self) -> None:
        """Settings relaunched on this simulator only, then read on its own
        until it shows the main screen."""
        run(["xcrun", "simctl", "terminate", self.udid, IOS_APP], timeout=60, check=False)
        time.sleep(1)
        run(["xcrun", "simctl", "launch", self.udid, IOS_APP], timeout=60)
        deadline = time.monotonic() + ROOT_WAIT_S
        while True:
            time.sleep(2)
            if self.shows("root"):
                return
            if time.monotonic() > deadline:
                raise StepFailed("Settings did not show its main screen after a relaunch")

    def registry(self) -> list[dict[str, Any]]:
        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise StepFailed(f"{self.state_file} is not a list")
        return [entry for entry in payload if isinstance(entry, dict)]


def pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_command(pid: int) -> str:
    return run(["ps", "-p", str(pid), "-o", "command="], timeout=30, check=False).stdout.strip()


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        stat = run(["ps", "-p", str(pid), "-o", "stat="], timeout=30, check=False)
        if stat.returncode != 0 or not stat.stdout.strip() or stat.stdout.strip().startswith("Z"):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)


def ios_tap_general(cli: Cli, ios: Ios) -> dict[str, dict[str, Any]]:
    reply = cli("ui tap --desc General", "ui", "tap", "--desc", "General", "--mode", "exact",
                pre=("--ios-hid", "idb"))
    payload = reply.payload
    screen = payload.get("screen") or {}
    foreground = payload.get("foreground") or {}
    labels = outline_labels(str(screen.get("outline") or ""))
    observe = {
        "exit": reply.code, "reply_ok": payload.get("ok"), "backend": payload.get("backend"),
        "outline_has_about": "About" in labels, "screen_count": screen.get("count"),
        "foreground": foreground, "changes_baseline": payload.get("changes_baseline"),
        "changes": change_counts(payload), "observe_error": payload.get("observe_error"),
        "warnings": warning_codes(payload),
        "independent_general_page": ios.shows("general"),
    }
    observe["ok"] = (reply.code == 0 and reply.ok and observe["outline_has_about"]
                     and foreground.get("app") == "Settings" and not observe["observe_error"]
                     and observe["independent_general_page"])
    tools = reply.tools
    taps = [index for index, entry in enumerate(tools)
            if entry.get("tool") == "idb" and tool_command(entry) == "ui tap"]
    first = taps[0] if taps else len(tools)
    single = {
        "describe_all_before_tap": count(tools[:first], "idb", "ui describe-all"),
        "idb_taps": len(taps),
        "describe_all_after_tap": count(tools[first + 1:], "idb", "ui describe-all"),
        "ui_quiet_after_tap": count(tools[first + 1:], "idb", "ui quiet"),
        "idb_calls": summary(tools),
    }
    single["ok"] = single["describe_all_before_tap"] == 1 and single["idb_taps"] == 1
    return {"observe": observe, "single_read": single}


def ios_quiet_settle(cli: Cli, ios: Ios) -> dict[str, dict[str, Any]]:
    tap = cli("ui tap --desc About --no-observe", "ui", "tap", "--desc", "About", "--mode",
              "exact", "--no-observe", pre=("--ios-hid", "idb"))
    wait = cli("ui wait --settled", "ui", "wait", "--settled")
    payload = wait.payload
    result = {
        "tap_ok": tap.ok, "tap_idb_taps": count(tap.tools, "idb", "ui tap"),
        "exit": wait.code, "settled": payload.get("settled"), "method": payload.get("method"),
        "snapshots": payload.get("snapshots"), "quiet_state": payload.get("quiet_state"),
        "quiet_fallback": payload.get("quiet_fallback"), "elapsed_ms": payload.get("elapsed_ms"),
        "ui_quiet_calls": count(wait.tools, "idb", "ui quiet"),
        "describe_all_calls": count(wait.tools, "idb", "ui describe-all"),
        "independent_about_page": ios.shows("about"),
    }
    result["ok"] = (tap.ok and result["tap_idb_taps"] == 1 and wait.code == 0
                    and payload.get("settled") is True and result["method"] == "idb-quiet"
                    and result["snapshots"] == 1 and result["ui_quiet_calls"] == 1
                    and result["describe_all_calls"] == 1)
    return {"quiet_settle": result}


def ios_axe_tap(cli: Cli, ios: Ios) -> dict[str, dict[str, Any]]:
    ios.to_root()
    reply = cli("--ios-hid axe ui tap --desc General", "ui", "tap", "--desc", "General",
                "--mode", "exact", pre=("--ios-hid", "axe"))
    tools = reply.tools
    touches = [index for index, entry in enumerate(tools)
               if entry.get("tool") == "axe" and tool_command(entry) == "touch"]
    argv = [str(item) for item in tools[touches[0]]["argv"]] if touches else []
    held = (bool(argv) and "--down" in argv and "--up" in argv
            and option_value(argv, "--delay") == AXE_HOLD
            and option_value(argv, "--udid") == ios.udid)
    first = touches[0] if touches else len(tools)
    result = {
        "exit": reply.code, "reply_ok": reply.payload.get("ok"),
        "backend": reply.payload.get("backend"), "axe_touches": len(touches),
        "axe_argv": argv, "held_touch": held,
        "axe_taps": count(tools, "axe", "tap"), "idb_taps": count(tools, "idb", "ui tap"),
        "describe_all_before_touch": count(tools[:first], "idb", "ui describe-all"),
        "independent_general_page": ios.shows("general"),
    }
    result["ok"] = (reply.code == 0 and reply.ok and result["backend"] == "axe"
                    and len(touches) == 1 and held and result["axe_taps"] == 0
                    and result["idb_taps"] == 0 and result["describe_all_before_touch"] == 1
                    and result["independent_general_page"])
    return {"axe_tap": result}


def ios_dead_companion(cli: Cli, ios: Ios) -> dict[str, dict[str, Any]]:
    before = ios.registry()
    others = [{"udid": entry.get("udid"), "pid": entry.get("pid"),
               "alive_before": pid_alive(entry.get("pid"))}
              for entry in before if entry.get("udid") != ios.udid]
    mine = [entry for entry in before
            if entry.get("udid") == ios.udid and entry.get("is_local") is True
            and pid_alive(entry.get("pid"))]
    if len(mine) != 1:
        raise StepFailed(f"{ios.state_file} lists {len(mine)} live local companions for "
                         f"{ios.udid}, not one; nothing was killed")
    pid = int(mine[0]["pid"])
    command = pid_command(pid)
    if "idb_companion" not in command or ios.udid not in command:
        raise StepFailed(f"pid {pid} does not run idb_companion for {ios.udid}; "
                         "nothing was killed")
    BUDGET.require(KILL_NEEDS_S, "the kill and the recovering ui tree")
    os.kill(pid, signal.SIGKILL)
    gone = wait_gone(pid)
    reply = cli("ui tree after the companion was killed", "ui", "tree")
    after = ios.registry()
    for other in others:
        other["alive_after"] = pid_alive(other["pid"])
    renewed = [entry.get("pid") for entry in after
               if entry.get("udid") == ios.udid and entry.get("is_local") is True]
    outline = str(reply.payload.get("outline") or "")
    result = {
        "killed_pids": [pid], "killed_command": command[:300], "gone": gone,
        "exit": reply.code, "reply_ok": reply.payload.get("ok"),
        "error_code": reply.payload.get("error_code"),
        "outline_lines": len(outline.splitlines()),
        "describe_all_calls": count(reply.tools, "idb", "ui describe-all"),
        "list_targets_calls": count(reply.tools, "idb", "list-targets"),
        "companion_pids_after": renewed, "other_companions": others,
    }
    result["ok"] = (gone and reply.code == 0 and reply.ok and result["outline_lines"] > 0
                    and all(other["alive_after"] == other["alive_before"] for other in others))
    return {"dead_companion": result}


def ios_ui_do(cli: Cli, ios: Ios) -> dict[str, dict[str, Any]]:
    ios.to_root()
    reply = cli("ui do (six-step walk-through)", "ui", "do", pre=("--ios-hid", "idb"),
                stdin=json.dumps(IOS_WALK), timeout=DO_TIMEOUT)
    payload = reply.payload
    steps = step_results(payload)
    result = {
        "exit": reply.code, "reply_ok": payload.get("ok"), "status": payload.get("status"),
        "steps": steps, "passed": sum(1 for step in steps if step.get("status") == "passed"),
        "failure": payload.get("failure"), "error_code": payload.get("error_code"),
        "idb_taps": count(reply.tools, "idb", "ui tap"),
        "foreground": payload.get("foreground"),
        "independent_main_screen": ios.shows("root"),
    }
    result["ok"] = (reply.code == 0 and reply.ok and result["status"] == "passed"
                    and len(steps) == len(IOS_WALK) and result["passed"] == len(IOS_WALK)
                    and result["idb_taps"] == len(IOS_WALK)
                    and result["independent_main_screen"])
    return {"ui_do": result}


def ios_scope(log: ToolLog, udid: str) -> dict[str, Any]:
    foreign = []
    for entry in log.entries():
        named = option_value([str(item) for item in entry.get("argv") or []], "--udid")
        if named is not None and named != udid:
            foreign.append(f"{entry.get('tool')} {tool_command(entry)} --udid {named}")
    return {"tool_calls": len(log.entries()), "other_udids": foreign, "ok": not foreign}


def run_ios(args: argparse.Namespace, report: dict[str, Any], home: Path,
            scratch: Path) -> None:
    real_idb = args.idb or shutil.which("idb")
    real_axe = args.axe or shutil.which("axe")
    report["preflight"] = preflight = {"idb": real_idb, "axe": real_axe}
    if not real_idb or not real_axe:
        raise StepFailed("idb and axe are both needed (pass --idb / --axe)")
    state_file = Path(os.environ.get("AUTONOM_IDB_STATE_FILE") or IDB_STATE_DEFAULT)
    ios = Ios(args.udid, real_axe, state_file)
    preflight["simulator_state"] = ios.state()
    if preflight["simulator_state"] != "Booted":
        raise StepFailed(f"simulator is {preflight['simulator_state']}; boot it first")
    report["environment"]["xcode"] = " ".join(
        line.strip() for line in run(["xcodebuild", "-version"], timeout=60,
                                     check=False).stdout.splitlines() if line.strip())
    log = ToolLog(scratch / "tool-calls.ndjson")
    wrappers = scratch / "bin"
    wrappers.mkdir()
    write_wrapper(wrappers, "idb", real_idb, log.path)
    write_wrapper(wrappers, "axe", real_axe, log.path)
    env = base_env(home)
    env["PATH"] = f"{wrappers}{os.pathsep}{env.get('PATH', '')}"
    cli = Cli(["--udid", args.udid], env, log)
    report["_cli"] = cli
    ios.to_root()
    preflight["main_screen"] = True
    preflight["ok"] = True
    # a start that fails or times out may still leave a session: stop it anyway
    report["session_attempted"] = True
    started = cli("session start --profile agent", "session", "start", "--profile", "agent",
                  "--app-id", IOS_APP, "--ui-backend", "idb")
    report["session_started"] = started.ok
    report["checks"]["session"] = session_check(started)
    if not started.ok:
        raise StepFailed(f"session start failed: {started.payload.get('error_code')} "
                         f"{started.payload.get('error')}")
    guarded(report, ("observe", "single_read"), lambda: ios_tap_general(cli, ios))
    guarded(report, ("quiet_settle",), lambda: ios_quiet_settle(cli, ios))
    guarded(report, ("axe_tap",), lambda: ios_axe_tap(cli, ios))
    guarded(report, ("dead_companion",), lambda: ios_dead_companion(cli, ios))
    guarded(report, ("ui_do",), lambda: ios_ui_do(cli, ios))
    report["checks"]["scope"] = ios_scope(log, args.udid)
    try:
        report["final_main_screen"] = ios.shows("root")
    except (StepFailed, subprocess.TimeoutExpired) as exc:
        report["final_main_screen"] = None
        report["final_read_error"] = str(exc)[-300:]


# --- Android (VER-004) -------------------------------------------------------------


class Android:
    def __init__(self, adb: str, serial: str) -> None:
        self.adb = adb
        self.serial = serial

    def shell(self, *argv: str, check: bool = True, timeout: float = 60) -> str:
        return run([self.adb, "-s", self.serial, "shell", *argv], timeout=timeout,
                   check=check).stdout

    def dump(self) -> tuple[set[str], bool]:
        """(labels, uiautomator said it could not get an idle state)."""
        text = run([self.adb, "-s", self.serial, "exec-out", "uiautomator", "dump", "/dev/tty"],
                   timeout=120, check=False).stdout
        labels = {html.unescape(value)
                  for value in re.findall(r' (?:text|content-desc)="([^"]*)"', text) if value}
        return labels, "could not get idle state" in text

    def shows(self, screen: str) -> bool:
        return ANDROID_SCREENS[screen](self.dump()[0])

    def airplane(self, timeout: float = 60) -> str:
        return self.shell("settings", "get", "global", "airplane_mode_on",
                          timeout=timeout).strip()

    def resumed(self) -> list[str]:
        text = self.shell("dumpsys", "activity", "activities", check=False)
        return [line.strip() for line in text.splitlines() if "ResumedActivity" in line][:2]

    def to_root(self) -> None:
        """Settings restarted on its main screen, then read on its own until
        the main list shows."""
        self.shell("am", "force-stop", ANDROID_APP, check=False)
        self.shell("am", "start", "-W", "-n", ANDROID_MAIN, timeout=120)
        deadline = time.monotonic() + ROOT_WAIT_S
        while True:
            time.sleep(2)
            if self.shows("root"):
                return
            if time.monotonic() > deadline:
                raise StepFailed("Settings did not show its main list after a restart")


def android_tap_network(cli: Cli, device: Android) -> dict[str, dict[str, Any]]:
    reply = cli('ui tap --text "Network & internet"', "ui", "tap", "--text",
                "Network & internet", "--mode", "exact")
    payload = reply.payload
    screen = payload.get("screen") or {}
    foreground = payload.get("foreground") or {}
    outline = str(screen.get("outline") or "")
    observe = {
        "exit": reply.code, "reply_ok": payload.get("ok"), "backend": payload.get("backend"),
        "outline_has_airplane_mode": "Airplane mode" in outline_labels(outline),
        "screen_count": screen.get("count"), "foreground": foreground,
        "changes_baseline": payload.get("changes_baseline"),
        "changes": change_counts(payload), "observe_error": payload.get("observe_error"),
        "warnings": warning_codes(payload),
        "independent_network_page": device.shows("network"),
    }
    observe["ok"] = (reply.code == 0 and reply.ok and observe["outline_has_airplane_mode"]
                     and foreground.get("app") == ANDROID_APP and not observe["observe_error"]
                     and observe["independent_network_page"])
    return {"observe": observe, "outline_state": outline_state_result(outline, "Airplane mode")}


def android_not_idle(cli: Cli, device: Android) -> dict[str, dict[str, Any]]:
    device.shell("am", "start", "-W", "-a", ANDROID_ABOUT_ACTION, timeout=120)
    time.sleep(2)
    resumed = device.resumed()
    labels, idle_error = device.dump()
    reply = cli("ui tree on the About page", "ui", "tree")
    result = {
        "resumed_activity": resumed, "independent_dump_idle_error": idle_error,
        "independent_labels": len(labels), "exit": reply.code,
        "error_code": reply.payload.get("error_code"),
        "hint": str(reply.payload.get("hint") or "")[:300],
    }
    result["ok"] = reply.code == 2 and result["error_code"] == "ui_not_idle"
    return {"not_idle": result}


def android_ui_do(cli: Cli, device: Android) -> dict[str, dict[str, Any]]:
    device.to_root()
    reply = cli("ui do (six-step walk-through)", "ui", "do",
                stdin=json.dumps(ANDROID_WALK), timeout=DO_TIMEOUT)
    payload = reply.payload
    steps = step_results(payload)
    result = {
        "exit": reply.code, "reply_ok": payload.get("ok"), "status": payload.get("status"),
        "steps": steps, "passed": sum(1 for step in steps if step.get("status") == "passed"),
        "failure": payload.get("failure"), "error_code": payload.get("error_code"),
        "foreground": payload.get("foreground"),
        "independent_main_list": device.shows("root"),
    }
    result["ok"] = (reply.code == 0 and reply.ok and result["status"] == "passed"
                    and len(steps) == len(ANDROID_WALK) and result["passed"] == len(ANDROID_WALK)
                    and result["independent_main_list"])
    return {"ui_do": result}


def run_android(args: argparse.Namespace, report: dict[str, Any], home: Path,
                scratch: Path) -> None:
    adb = args.adb or shutil.which("adb")
    report["preflight"] = preflight = {"adb": adb}
    if not adb:
        raise StepFailed("adb not found (pass --adb)")
    device = Android(adb, args.serial)
    preflight["state"] = run([adb, "-s", args.serial, "get-state"], timeout=30,
                             check=False).stdout.strip()
    if preflight["state"] != "device":
        raise StepFailed(f"{args.serial} is {preflight['state'] or 'not attached'}")
    preflight["boot_completed"] = device.shell("getprop", "sys.boot_completed").strip()
    if preflight["boot_completed"] != "1":
        raise StepFailed(f"{args.serial} has not finished booting")
    helper = device.shell("pgrep", "-f", "mobilecli", check=False).strip()
    preflight["mobilecli_helper"] = bool(helper)
    if helper:
        raise StepFailed("a mobilecli device server runs on this emulator and holds "
                         "UiAutomation; stop it first (nothing was changed)")
    report["airplane_mode_before"] = device.airplane()
    report["environment"]["adb"] = run([adb, "version"], timeout=30,
                                       check=False).stdout.splitlines()[:1]
    env = base_env(home)
    if args.adb:
        env["AUTONOM_ADB"] = args.adb
    cli = Cli(["--serial", args.serial], env, None)
    report["_cli"] = cli
    device.to_root()
    preflight["main_screen"] = True
    preflight["ok"] = True
    # a start that fails or times out may still leave a session: stop it anyway
    report["session_attempted"] = True
    started = cli("session start --profile agent", "session", "start", "--profile", "agent",
                  "--app-id", ANDROID_APP)
    report["session_started"] = started.ok
    report["checks"]["session"] = session_check(started)
    if not started.ok:
        raise StepFailed(f"session start failed: {started.payload.get('error_code')} "
                         f"{started.payload.get('error')}")
    guarded(report, ("observe", "outline_state"), lambda: android_tap_network(cli, device))
    guarded(report, ("not_idle",), lambda: android_not_idle(cli, device))
    guarded(report, ("ui_do",), lambda: android_ui_do(cli, device))
    try:
        report["final_main_list"] = device.shows("root")
    except (StepFailed, subprocess.TimeoutExpired) as exc:
        report["final_main_list"] = None
        report["final_read_error"] = str(exc)[-300:]


def finish_android(args: argparse.Namespace, report: dict[str, Any]) -> None:
    adb = (report.get("preflight") or {}).get("adb")
    before = report.get("airplane_mode_before")
    if not adb or before is None:
        return
    try:
        after = Android(adb, args.serial).airplane(timeout=FINAL_READ_TIMEOUT)
    except (StepFailed, subprocess.TimeoutExpired, OSError) as exc:
        after = None
        report["airplane_error"] = str(exc)[-300:]
    report["checks"]["airplane_mode"] = {"before": before, "after": after,
                                         "ok": before == "0" and after == "0"}


# --- main --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_loop_live.py",
        description="Live check of the agent loop (agent profile, observe after an action, "
                    "ui do and the iOS/Android read fixes) on one booted bench device: "
                    "VER-003 on an iOS Simulator, VER-004 on an Android emulator. It drives "
                    "Settings on that device only; use a private test device.",
        epilog="Example: agent_loop_live.py --platform ios --udid <UDID> --evidence-dir DIR. "
               "Writes DIR/agent-loop-<platform>.json; exit 0 when every check passed.")
    parser.add_argument("--platform", required=True, choices=("ios", "android"))
    parser.add_argument("--udid", help="the iOS bench simulator (with --platform ios)")
    parser.add_argument("--serial", help="the Android bench emulator (with --platform android)")
    parser.add_argument("--evidence-dir", required=True, type=Path,
                        help="where agent-loop-<platform>.json is written")
    parser.add_argument("--idb", help="the real idb client (default: idb on PATH)")
    parser.add_argument("--axe", help="the real AXe binary (default: axe on PATH)")
    parser.add_argument("--adb", help="the adb binary (default: adb on PATH)")
    parser.add_argument("--keep-home", action="store_true",
                        help="keep the temporary AUTONOM_HOME and the idb/AXe call log")
    parser.add_argument("--time-limit", type=float, default=TIME_LIMIT_S, metavar="SECONDS",
                        help=f"the whole run ends within this, cleanup included (default "
                             f"{TIME_LIMIT_S}; VER-003 and VER-004 allow 1800); the last "
                             f"{CLEANUP_RESERVE_S} s are kept for the cleanup")
    return parser


SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def set_handlers(handler: Any) -> dict[int, Any]:
    """``handler`` for SIGTERM and SIGHUP; returns the handlers it replaced
    (none off the main thread, where Python cannot set one)."""
    previous: dict[int, Any] = {}
    for signum in SIGNALS:
        try:
            previous[signum] = signal.signal(signum, handler)
        except (ValueError, OSError):
            break
    return previous


def save_report(report: dict[str, Any], out: Path, platform: str) -> None:
    """Decide ``ok`` from what the report holds now and write it."""
    checks = report["checks"]
    missing = [name for name in EXPECTED_CHECKS[platform] if name not in checks]
    if missing:
        report["checks_missing"] = missing
    else:
        report.pop("checks_missing", None)
    report["ok"] = bool(
        (report.get("preflight") or {}).get("ok") and "error" not in report
        and report.get("cleanup") == "done" and not report.get("cleanup_errors")
        and report.get("session_stopped") is True and not missing
        and all(check.get("ok") for check in checks.values()))
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cleanup_step(report: dict[str, Any], name: str, action: Callable[[], None]) -> None:
    """One cleanup action; a failure is recorded and the next one still runs."""
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - every cleanup action must get its turn
        report.setdefault("cleanup_errors", {})[name] = f"{type(exc).__name__}: {exc}"[-300:]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.platform == "ios" and (not args.udid or args.serial):
        parser.error("--platform ios takes --udid (and no --serial)")
    if args.platform == "android" and (not args.serial or args.udid):
        parser.error("--platform android takes --serial (and no --udid)")
    if not args.time_limit > CLEANUP_RESERVE_S:
        parser.error(f"--time-limit must be more than the {CLEANUP_RESERVE_S} s kept "
                     "for the cleanup")
    BUDGET.start(args.time_limit)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    out = args.evidence_dir / f"agent-loop-{args.platform}.json"
    report: dict[str, Any] = {
        "case": "VER-003" if args.platform == "ios" else "VER-004",
        "platform": args.platform, "device": args.udid or args.serial,
        "started_at": now(), "host_load_start": host_load(),
        "time_limit_s": args.time_limit,
        "environment": {"python": sys.version.split()[0]},
        "checks": {}, "ok": False,
    }
    home = Path(tempfile.mkdtemp(prefix="autonom-agent-loop-"))
    scratch = Path(tempfile.mkdtemp(prefix="autonom-agent-loop-tools-"))
    report["autonom_home"] = str(home)
    exit_code: int | None = None
    # a runner's timeout sends SIGTERM: without a handler the process would
    # end at once, leaving the session, the temporary home and no report
    previous = set_handlers(_raise_terminated)
    try:
        if args.platform == "ios":
            run_ios(args, report, home, scratch)
        else:
            run_android(args, report, home, scratch)
    except (StepFailed, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[-400:]
    except Terminated as exc:
        report["error"] = f"Terminated: {exc}"
        report["terminated"] = signal.Signals(exc.signum).name
        exit_code = 128 + exc.signum
    except BaseException as exc:
        # an interrupt or a defect of this script: the report still says
        # why and is never ok; the exception goes on after the cleanup
        report["error"] = f"{type(exc).__name__}: {exc}"[-400:]
        raise
    finally:
        set_handlers(_note_late_signal)
        BUDGET.end_steps()  # the cleanup calls use their own short timeouts
        report["out_of_time"] = BUDGET.exhausted
        cli: Cli | None = report.pop("_cli", None)
        if cli is not None:
            report["calls"] = cli.calls  # the same list: later calls show in the last save
        # written before the cleanup, so a SIGKILL during it still leaves evidence
        report["cleanup"] = "pending"
        cleanup_step(report, "save_before_cleanup",
                     lambda: save_report(report, out, args.platform))
        if cli is not None and report.get("session_attempted"):
            def stop_session() -> None:
                stopped = cli("session stop", "session", "stop", timeout=STOP_TIMEOUT)
                report["session_stopped"] = stopped.ok
            cleanup_step(report, "session_stop", stop_session)
        if cli is not None:
            report["checks"]["one_line"] = one_line_check(cli)
        if args.platform == "android":
            cleanup_step(report, "airplane_mode", lambda: finish_android(args, report))
        if args.keep_home:
            report["kept"] = {"autonom_home": str(home), "tool_log_dir": str(scratch)}
        else:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(scratch, ignore_errors=True)
            report["home_removed"] = not home.exists()
        report["host_load_end"] = host_load()
        report["finished_at"] = now()
        report["cleanup"] = "done"
        save_report(report, out, args.platform)
        print(json.dumps({"ok": report["ok"], "report": str(out),
                          "checks": {name: check.get("ok")
                                     for name, check in report["checks"].items()}},
                         separators=(",", ":")))
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if exit_code is not None:
        return exit_code
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
