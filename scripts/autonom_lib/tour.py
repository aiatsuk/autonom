"""`autonom tour` — the guided first run.

A newcomer (human or agent) asks three things of a harness: what can it do,
how do I drive it, and can it show me on *my* machine. The tour answers all
three with one verb: an overview of the verb families, the workflow they
compose into, an inventory of the Android emulators and iOS simulators this
Mac has, and an offer — boot one, own a session, walk three screens into the
Settings app with screenshots, a UI hierarchy and the device log captured
on each action itself, then hand back the session directory, the HTML report
and a written account of what happened.

The walk is an ordinary Flow v1 file shipped next to this module, run with
evidence mode `always`, so everything the tour produces is the same
evidence the harness produces for real work — nothing is special-cased.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import emulator as emulator_mod
from . import errors, ios_idb, ios_simctl
from . import platform as platform_mod
from . import report_bundle as report_bundle_mod
from . import session as session_mod
from .flow import executor as flow_executor
from .flow import report as flow_report
from .flow import validator as flow_validator
from .platform import ANDROID, IOS, Target

TOURS_DIR = Path(__file__).resolve().parent / "tours"
BUILT_IN = {
    ANDROID: {
        "flow": TOURS_DIR / "settings_android.yaml",
        "app_id": "com.android.settings",
        "title": "Settings → Network & internet → Internet",
    },
    IOS: {
        "flow": TOURS_DIR / "settings_ios.yaml",
        "app_id": "com.apple.Preferences",
        "title": "Settings → General → About",
    },
}

OVERVIEW = [
    {"area": "Targets", "verbs": "devices, devices boot|shutdown, doctor, capabilities",
     "what": "one inventory of Android emulators and iOS simulators, boot and shut them "
             "down, and an honest answer to what this machine can do"},
    {"area": "Session", "verbs": "session start|launch|force-stop|clear|stop, journal, note",
     "what": "own one explicit target; every verb and note is journaled under "
             "~/.autonom/sessions/<id>/"},
    {"area": "Screen", "verbs": "ui tree|find|tap|swipe|type|key, screenshot, shots, record",
     "what": "a compact accessibility tree on both platforms, semantic taps, and "
             "screenshots with provenance embedded in the PNG"},
    {"area": "State", "verbs": "open, permissions, location, media, file, simulator …",
     "what": "deep links, permissions, location, media, container files, battery, "
             "appearance, status bar, keyboard pinning"},
    {"area": "Network", "verbs": "network start|attach|requests|mock|export|stop",
     "what": "consent-gated HTTPS capture and response mocking through mitmproxy, HAR export"},
    {"area": "Flows", "verbs": "flow check|run|create|import|export, teach, app-skill, proof",
     "what": "repeatable Flow v1 files with polling assertions, per-step evidence, a "
             "repair brief on failure, Maestro import/export, PR proof"},
    {"area": "Evidence", "verbs": "report build|export|suite|serve, replay, ci, agent, atlas",
     "what": "HTML/JUnit/Allure reports, integrity-checked bundles, prefix replay, "
             "campaign CI, the observed screen graph"},
    {"area": "Metrics", "verbs": "metrics snapshot|series|memory|frames|trace",
     "what": "memory and CPU snapshots, directional growth, frame stats, "
             "simpleperf/xctrace traces — measured, never claimed"},
]

HOW_TO = [
    "autonom doctor — confirm adb / simctl / idb are there and see what is ready",
    "autonom devices — pick one target; boot it with devices boot if it is down",
    "autonom session start --serial <id> --app-id <pkg> — own the target and an artifacts dir",
    "autonom ui tree, ui find, ui tap — read the screen, act by label or id, never by guess",
    "autonom screenshot --label … — evidence with provenance; compare before/after",
    "write a flow (autonom flow create --from-session current) and run it with evidence",
    "autonom report build — HTML + JUnit + bundle for the run; autonom session stop when done",
]


# --- inventory and proposal --------------------------------------------------


def inventory(args: argparse.Namespace | None = None) -> dict[str, Any]:
    devices, warnings = platform_mod.list_all(args)
    android = [d for d in devices if d.get("platform") == ANDROID]
    ios = [d for d in devices if d.get("platform") == IOS]
    avds: list[str] = []
    adb_path = None
    try:
        adb_path = adb_mod.find_adb(getattr(args, "adb", None) if args else None)
        emulator_bin = emulator_mod.find_emulator(
            getattr(args, "emulator", None) if args else None, adb_path=adb_path)
        avds = emulator_mod.list_avds(emulator_bin)
    except errors.AutonomError:
        pass
    if adb_path:
        # Name a running emulator by its AVD, as `devices` does.
        for device in android:
            if device.get("running") and str(device.get("target_id", "")).startswith("emulator-"):
                name = emulator_mod.running_avd_name(adb_path, device["target_id"])
                if name:
                    device["avd"] = name
    idb_ready = False
    if ios:
        try:
            idb_ready = ios_idb.probe(getattr(args, "idb", None) if args else None,
                                      None).get("state") == "ready"
        except Exception:  # noqa: BLE001 - inventory never fails on a probe
            idb_ready = False
    return {
        "android": {
            "running": [d for d in android if d.get("running")],
            "attached": [d for d in android if not d.get("running")],
            "avds": avds,
        },
        "ios": {
            "booted": [d for d in ios if d.get("running")],
            "available": [d for d in ios if not d.get("running")],
            "idb_ready": idb_ready,
        },
        "warnings": warnings,
    }


def _prefer_iphone(simulators: list[dict[str, Any]]) -> dict[str, Any] | None:
    phones = [s for s in simulators if "iphone" in str(s.get("name", "")).lower()]
    pool = phones or simulators
    if not pool:
        return None
    # Newest runtime first; `iOS 26.5` sorts above `iOS 18.2` by its numbers.
    def key(item: dict[str, Any]) -> tuple:
        numbers = [int(part) for part in str(item.get("runtime", "")).replace(".", " ").split()
                   if part.isdigit()]
        return tuple(numbers), str(item.get("name"))
    return sorted(pool, key=key, reverse=True)[0]


def choose(inv: dict[str, Any], platform: str | None = None,
           avd: str | None = None) -> dict[str, Any] | None:
    """The target the tour would use, in order of least disruption:
    a running emulator, a booted simulator, an AVD to boot, a simulator to boot."""
    candidates: list[dict[str, Any]] = []
    if platform in (None, ANDROID):
        for device in inv["android"]["running"]:
            if str(device.get("target_id", "")).startswith("emulator-"):
                candidates.append({"platform": ANDROID, "target_id": device["target_id"],
                                   "name": device.get("avd") or device.get("name"),
                                   "boot_needed": False})
    if platform in (None, IOS) and inv["ios"]["idb_ready"]:
        booted = _prefer_iphone(inv["ios"]["booted"])
        if booted:
            candidates.append({"platform": IOS, "target_id": booted["target_id"],
                               "name": f"{booted.get('name')} ({booted.get('runtime')})",
                               "boot_needed": False})
    if platform in (None, ANDROID):
        names = inv["android"]["avds"]
        if avd and avd in names:
            names = [avd]
        if names:
            candidates.append({"platform": ANDROID, "avd": names[0], "name": names[0],
                               "boot_needed": True})
    if platform in (None, IOS) and inv["ios"]["idb_ready"]:
        cold = _prefer_iphone(inv["ios"]["available"])
        if cold:
            candidates.append({"platform": IOS, "target_id": cold["target_id"],
                               "name": f"{cold.get('name')} ({cold.get('runtime')})",
                               "boot_needed": True})
    return candidates[0] if candidates else None


def flow_labels(flow_path: Path) -> list[dict[str, Any]]:
    flow = flow_validator.validate_tree(flow_path)
    return [{"index": index, "command": step.command, "label": step.label}
            for index, step in enumerate(flow.steps, start=1)]


def proposal(choice: dict[str, Any] | None, flow_override: Path | None,
             inv: dict[str, Any]) -> dict[str, Any]:
    if choice is None:
        reasons = []
        if not inv["android"]["running"] and not inv["android"]["avds"]:
            reasons.append("no Android emulator is running and no AVD exists")
        if inv["ios"]["booted"] or inv["ios"]["available"]:
            if not inv["ios"]["idb_ready"]:
                reasons.append("iOS simulators exist but idb is not ready, and the walk "
                               "needs it to tap")
        else:
            reasons.append("no iOS simulator is available")
        return {"available": False, "reasons": reasons,
                "hint": "Create an AVD in Android Studio or install Xcode simulators, "
                        "run 'autonom doctor', then 'autonom tour' again."}
    built_in = BUILT_IN[choice["platform"]]
    flow_path = flow_override or built_in["flow"]
    flow = flow_validator.validate_tree(flow_path)
    command = ["autonom", "tour", "--run", "--platform", choice["platform"]]
    if choice.get("avd"):
        command += ["--avd", choice["avd"]]
    elif choice.get("target_id"):
        command += ["--target", choice["target_id"]]
    return {
        "available": True,
        "platform": choice["platform"],
        "device": choice,
        "app_id": flow.app_id or built_in["app_id"],
        "title": built_in["title"] if flow_override is None else flow.name,
        "flow": str(flow_path),
        "steps": flow_labels(flow_path),
        "evidence": "screenshots + UI hierarchy + device log attached to every step",
        "run_command": " ".join(command),
    }


# --- the walk ------------------------------------------------------------------


def _boot(choice: dict[str, Any], args: argparse.Namespace) -> tuple[Target, dict[str, Any]]:
    detail: dict[str, Any] = {"booted_by_tour": False}
    if choice["platform"] == ANDROID:
        adb_path = adb_mod.find_adb(getattr(args, "adb", None))
        if choice.get("boot_needed"):
            emulator_bin = emulator_mod.find_emulator(getattr(args, "emulator", None),
                                                      adb_path=adb_path)
            booted = emulator_mod.boot_avd(emulator_bin, adb_path, choice["avd"],
                                           wait=True, timeout=240)
            choice = {**choice, "target_id": booted["target_id"]}
            detail.update({"booted_by_tour": True, "avd": choice["avd"],
                           "boot": booted})
        target = platform_mod._android_target(choice["target_id"], adb_path)  # noqa: SLF001
        return target, detail
    xcrun = ios_simctl.find_simctl(getattr(args, "simctl", None))
    if choice.get("boot_needed"):
        detail["booted_by_tour"] = ios_simctl.boot(xcrun, choice["target_id"], timeout=180)
    target = platform_mod._ios_target(choice["target_id"], xcrun)  # noqa: SLF001
    return target, detail


def _evidence_for(run_dir: Path, shots_dir: Path, index: int) -> dict[str, str | None]:
    after = sorted(shots_dir.glob(f"*step-{index}-after.png"))
    before = sorted(shots_dir.glob(f"*step-{index}-before.png"))
    flow_shot = sorted(shots_dir.glob(f"*_flow*.png"))
    hierarchy = run_dir / f"step-{index}-after-hierarchy.json"
    logs = run_dir / f"step-{index}-after-logs.txt"
    return {
        "screenshot": str(after[-1]) if after else None,
        "screenshot_before": str(before[-1]) if before else None,
        "hierarchy": str(hierarchy) if hierarchy.exists() else None,
        "logs": str(logs) if logs.exists() else None,
    }


def narrative(run: dict[str, Any]) -> str:
    lines = [f"# Autonom tour — {run['title']}", ""]
    device = run["device"]
    lines.append(f"**Device:** {device.get('name')} (`{run['target_id']}`, {run['platform']})"
                 + ("  — booted by the tour" if run.get('booted_by_tour') else "  — already running"))
    lines.append(f"**App:** `{run['app_id']}`")
    lines.append(f"**Session:** `{run['session_id']}` → `{run['artifacts_dir']}`")
    lines.append(f"**Result:** {run['status']} in {run['duration_ms']} ms")
    lines.append("")
    lines.append("## What was done")
    lines.append("")
    for step in run["steps"]:
        mark = {"passed": "✅", "failed": "❌", "skipped": "⏭"}.get(step["status"], "•")
        head = f"{mark} **Step {step['index']}** — {step.get('label') or step['command']}"
        lines.append(head + f" (`{step['command']}`, {step.get('duration_ms', 0)} ms)")
        if step.get("screenshot"):
            lines.append(f"   - screenshot: `{step['screenshot']}`")
        if step.get("hierarchy"):
            lines.append(f"   - UI hierarchy: `{step['hierarchy']}`")
        if step.get("logs"):
            lines.append(f"   - device log: `{step['logs']}`")
        if step.get("error"):
            lines.append(f"   - error: {step['error']}")
    lines.append("")
    lines.append("## Where everything is")
    lines.append("")
    lines.append(f"- session directory: `{run['artifacts_dir']}`")
    lines.append(f"- this run: `{run['run_dir']}` (events.ndjson, manifest.json, per-step hierarchy and logs)")
    lines.append(f"- screenshots: `{run['shots_dir']}`")
    lines.append(f"- HTML report: `{run['report_html']}`  (open it in a browser)")
    lines.append(f"- JUnit: `{run['report_junit']}`, bundle: `{run['report_bundle']}`")
    lines.append(f"- journal of every verb: `{run['journal']}`")
    if run.get("failure"):
        lines.append("")
        lines.append("## The step that failed")
        lines.append("")
        lines.append(f"`{run['failure'].get('command')}` at line {run['failure'].get('line')}: "
                     f"{run['failure'].get('error')}")
        if run.get("repair"):
            lines.append("")
            lines.append("Repair brief:")
            for command in run["repair"].get("commands", []):
                lines.append(f"- `{command}`")
    lines.append("")
    lines.append("## Next")
    lines.append("")
    lines.append(f"- `autonom report open --session {run['session_id']}` — the interactive report")
    lines.append(f"- `autonom journal --session-id {run['session_id']}` — every action, in order")
    lines.append("- write your own flow: `autonom flow create --from-session <id>` after a manual session")
    if run.get("booted_by_tour") and not run.get("shutdown"):
        lines.append(f"- the device is still up; `autonom devices shutdown --target {run['target_id']}` powers it off")
    return "\n".join(lines) + "\n"


def run(choice: dict[str, Any], args: argparse.Namespace, *,
        flow_override: Path | None = None, shutdown: bool = False) -> dict[str, Any]:
    from .flow import repair as flow_repair

    started = time.monotonic()
    target, boot_detail = _boot(choice, args)
    built_in = BUILT_IN[target.platform]
    flow_path = flow_override or built_in["flow"]
    flow = flow_validator.validate_tree(flow_path)
    app_id = flow.app_id or built_in["app_id"]

    tooling: dict[str, Any] = {}
    if target.platform == IOS:
        tooling["simctl"] = target.tool
        tooling["idb"] = ios_idb.probe(getattr(args, "idb", None), None)
        if tooling["idb"].get("state") != "ready":
            raise errors.AutonomError(
                errors.IDB_REQUIRED,
                "the iOS walk taps by accessibility label, which needs idb",
                "Install idb (brew install idb-companion; pipx install fb-idb) or run the "
                "tour on Android: autonom tour --run --platform android.")
    else:
        tooling["adb"] = target.tool
    record = session_mod.start_session(
        target.tool, serial=target.serial, app_id=app_id,
        platform=target.platform, target_id=target.target_id, tooling=tooling)

    config = flow_executor.RunConfig(
        evidence_mode="always", evidence_collect=("screenshot", "hierarchy", "logs"))
    runner = flow_executor.Executor(target, record, config)
    try:
        result = runner.run(flow)
    except errors.AutonomError as exc:
        # An infrastructure failure (the first real run met a hung
        # `uiautomator dump`) must not leave the tour's own session dangling
        # behind the envelope; close it and say where the partial evidence is.
        session_mod.stop_session()
        exc.extra.update({"session_id": record["session_id"],
                          "artifacts_dir": record["artifacts_dir"]})
        raise

    run_dir = Path(result.events_path).parent
    artifacts_dir = Path(record["artifacts_dir"])
    shots_dir = artifacts_dir / "shots" / result.run_id
    manifest = flow_report.load_manifest(run_dir)
    html_path = run_dir / "report.html"
    html_path.write_text(flow_report.render_html(manifest, artifacts_dir), encoding="utf-8")
    junit_path = run_dir / "report.xml"
    junit_path.write_text(flow_report.render_junit(manifest), encoding="utf-8")
    bundle = report_bundle_mod.build(manifest, artifacts_root=artifacts_dir,
                                     out=run_dir / "bundle-v2")

    steps = []
    for outcome in result.steps:
        entry = {"index": outcome.index, "command": outcome.command, "label": outcome.label,
                 "status": outcome.status, "duration_ms": outcome.duration_ms,
                 "error": outcome.error, "error_code": outcome.error_code}
        entry.update(_evidence_for(run_dir, shots_dir, outcome.index))
        steps.append({k: v for k, v in entry.items() if v is not None})

    summary: dict[str, Any] = {
        "status": result.status,
        "title": built_in["title"] if flow_override is None else flow.name,
        "platform": target.platform,
        "target_id": target.target_id,
        "device": {**choice, "target_id": target.target_id},
        "app_id": app_id,
        "session_id": record["session_id"],
        "artifacts_dir": str(artifacts_dir),
        "run_id": result.run_id,
        "run_dir": str(run_dir),
        "shots_dir": str(shots_dir),
        "journal": str(artifacts_dir / "journal.ndjson"),
        "report_html": str(html_path),
        "report_junit": str(junit_path),
        "report_bundle": bundle["bundle"],
        "steps": steps,
        "duration_ms": int((time.monotonic() - started) * 1000),
        **boot_detail,
    }
    if result.failure:
        summary["failure"] = result.failure
        brief = flow_repair.repair_brief(str(flow_path), result.failure,
                                         [{"index": s["index"], "selector": None} for s in steps],
                                         events_path=result.events_path)
        if brief:
            summary["repair"] = brief

    if shutdown and boot_detail.get("booted_by_tour"):
        if target.platform == ANDROID:
            emulator_mod.kill_emulator(target.tool, target.target_id)
        else:
            ios_simctl.shutdown(target.tool, target.target_id)
        summary["shutdown"] = True

    session_mod.stop_session()
    summary["session_stopped"] = True
    text = narrative(summary)
    tour_md = run_dir / "tour.md"
    tour_md.write_text(text, encoding="utf-8")
    summary["tour_md"] = str(tour_md)
    summary["narrative"] = text
    return summary


# --- entry point ---------------------------------------------------------------


def overview_text(payload: dict[str, Any]) -> str:
    lines = ["# Autonom — what it is, how to use it, what this Mac has", ""]
    lines.append("## What it does")
    lines.append("")
    for item in payload["overview"]:
        lines.append(f"- **{item['area']}** (`{item['verbs']}`): {item['what']}")
    lines.append("")
    lines.append("## The usual workflow")
    lines.append("")
    for index, item in enumerate(payload["how_to"], start=1):
        lines.append(f"{index}. {item}")
    lines.append("")
    inv = payload["targets"]
    lines.append("## On this machine")
    lines.append("")
    lines.append(f"- Android: {len(inv['android']['running'])} running emulator(s), "
                 f"{len(inv['android']['avds'])} AVD(s) to boot")
    lines.append(f"- iOS: {len(inv['ios']['booted'])} booted, {len(inv['ios']['available'])} "
                 f"available simulator(s); idb {'ready' if inv['ios']['idb_ready'] else 'not ready'}")
    lines.append("")
    prop = payload["proposal"]
    lines.append("## The offer")
    lines.append("")
    if prop.get("available"):
        device = prop["device"]
        verb = "boot" if device.get("boot_needed") else "use the running"
        lines.append(f"I can {verb} **{device.get('name')}** ({prop['platform']}), own a session, "
                     f"and walk {prop['title']} — a screenshot, the UI hierarchy and the device "
                     f"log at every step — then hand you the session directory and a report.")
        lines.append("")
        for step in prop["steps"]:
            lines.append(f"{step['index']}. {step.get('label') or step['command']}")
        lines.append("")
        lines.append(f"Run it: `{prop['run_command']}`")
    else:
        lines.append("Nothing to walk on yet: " + "; ".join(prop.get("reasons", [])))
        lines.append(prop.get("hint", ""))
    return "\n".join(lines) + "\n"


def command(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Build the overview payload and decide whether to run the walk."""
    flow_override = Path(args.flow).expanduser() if getattr(args, "flow", None) else None
    inv = inventory(args)
    explicit = getattr(args, "target", None) or getattr(args, "serial", None) \
        or getattr(args, "udid", None)
    platform = getattr(args, "platform", None)
    if explicit:
        matched = [d for d in inv["android"]["running"] + inv["ios"]["booted"]
                   + inv["android"]["attached"] + inv["ios"]["available"]
                   if d.get("target_id") == explicit]
        if not matched:
            raise errors.AutonomError(errors.NO_TARGET, f"no such target: {explicit}",
                                      "Run 'autonom devices' to list targets.")
        device = matched[0]
        choice = {"platform": device["platform"], "target_id": explicit,
                  "name": device.get("avd") or device.get("name"),
                  "boot_needed": not device.get("running")}
    else:
        choice = choose(inv, platform, getattr(args, "avd", None))
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "overview",
        "overview": OVERVIEW,
        "how_to": HOW_TO,
        "targets": inv,
        "proposal": proposal(choice, flow_override, inv),
    }
    wants_run = bool(getattr(args, "run", False))
    if not wants_run and sys.stdin is not None and sys.stdin.isatty() \
            and payload["proposal"].get("available"):
        print(overview_text(payload), file=sys.stderr)
        print("Run the walk now? [y/N] ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip().lower()
        wants_run = answer in ("y", "yes", "д", "да")
    if wants_run:
        if not payload["proposal"].get("available"):
            raise errors.AutonomError(
                errors.NO_TARGET, "nothing to run the tour on",
                payload["proposal"].get("hint", "Run 'autonom devices'."),
                reasons=payload["proposal"].get("reasons", []))
        payload["mode"] = "run"
        payload["run"] = run(choice, args, flow_override=flow_override,
                             shutdown=bool(getattr(args, "shutdown", False)))
        return payload, payload["run"]["narrative"]
    return payload, overview_text(payload)
