"""`autonom tour` — the guided first run.

A newcomer (human or agent) asks three things of a harness: what can it do,
how do I drive it, and can it show me on *my* machine. The tour answers all
three with one verb: an overview of the verb families, the workflow they
compose into, an inventory of the Android emulators and iOS simulators this
Mac has, and an offer — boot one, own a session, walk three screens into the
Settings app with a screenshot and a UI hierarchy captured after each step
(and the device log wherever the step produced log lines), then hand back
the session directory, the HTML report and a written account of what
happened.

The walk is an ordinary Flow v1 file shipped next to this module, run with
evidence mode `always`, so everything the tour produces is the same
evidence the harness produces for real work — nothing is special-cased.
"""
from __future__ import annotations

import argparse
import os
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


def _by_preference(simulators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """iPhones before other simulators, newest runtime first; `iOS 26.5`
    sorts above `iOS 18.2` by its numbers."""
    def key(item: dict[str, Any]) -> tuple:
        numbers = [int(part) for part in str(item.get("runtime", "")).replace(".", " ").split()
                   if part.isdigit()]
        return "iphone" in str(item.get("name", "")).lower(), tuple(numbers), str(item.get("name"))
    return sorted(simulators, key=key, reverse=True)


def _prefer_iphone(simulators: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = _by_preference(simulators)
    return ordered[0] if ordered else None


def _sim_choice(sim: dict[str, Any], boot_needed: bool) -> dict[str, Any]:
    return {"platform": IOS, "target_id": sim["target_id"],
            "name": f"{sim.get('name')} ({sim.get('runtime')})", "boot_needed": boot_needed}


def _ready_candidates(inv: dict[str, Any], platform: str | None) -> list[dict[str, Any]]:
    """Targets that are up already: running emulators, then booted simulators."""
    found: list[dict[str, Any]] = []
    if platform in (None, ANDROID):
        for device in inv["android"]["running"]:
            if str(device.get("target_id", "")).startswith("emulator-"):
                found.append({"platform": ANDROID, "target_id": device["target_id"],
                              "name": device.get("avd") or device.get("name"),
                              "boot_needed": False})
    if platform in (None, IOS) and inv["ios"]["idb_ready"]:
        found.extend(_sim_choice(sim, False) for sim in _by_preference(inv["ios"]["booted"]))
    return found


def _bootable_candidates(inv: dict[str, Any], platform: str | None) -> list[dict[str, Any]]:
    """Targets the tour would have to boot: AVDs, then shut-down simulators."""
    found: list[dict[str, Any]] = []
    if platform in (None, ANDROID):
        found.extend({"platform": ANDROID, "avd": name, "name": name, "boot_needed": True}
                     for name in inv["android"]["avds"])
    if platform in (None, IOS) and inv["ios"]["idb_ready"]:
        found.extend(_sim_choice(sim, True) for sim in _by_preference(inv["ios"]["available"]))
    return found


def run_command(choice: dict[str, Any]) -> str:
    command = ["autonom", "tour", "--run", "--platform", choice["platform"]]
    if choice.get("avd"):
        command += ["--avd", choice["avd"]]
    elif choice.get("target_id"):
        command += ["--target", choice["target_id"]]
    return " ".join(command)


def choose(inv: dict[str, Any], platform: str | None = None,
           avd: str | None = None, *, strict: bool = False) -> dict[str, Any] | None:
    """The target the tour would use, in order of least disruption:
    a running emulator, a booted simulator, an AVD to boot, a simulator to boot.

    An explicit `--avd` is honoured or refused, never swapped for another
    device. With `strict` (an explicit `--run`), several candidates in the
    first non-empty tier are an ambiguity error listing them — the walk owns
    a device and must not guess which one; the overview still offers the
    first."""
    if avd:
        if platform == IOS:
            raise errors.AutonomError(
                errors.CONFLICTING_TARGET_FLAGS,
                f"--avd {avd} names an Android emulator, but --platform is ios",
                "Drop --avd, or use --platform android.")
        for device in inv["android"]["running"]:
            if device.get("avd") == avd and str(device.get("target_id", "")).startswith("emulator-"):
                return {"platform": ANDROID, "target_id": device["target_id"], "name": avd,
                        "boot_needed": False}
        names = inv["android"]["avds"]
        if avd not in names:
            raise errors.AutonomError(
                errors.AVD_NOT_FOUND, f"AVD '{avd}' does not exist",
                ("AVDs on this Mac: " + ", ".join(names)) if names
                else "No AVD exists; create one in Android Studio's Device Manager.",
                avds=list(names))
        return {"platform": ANDROID, "avd": avd, "name": avd, "boot_needed": True}
    for tier in (_ready_candidates(inv, platform), _bootable_candidates(inv, platform)):
        if not tier:
            continue
        if strict and len(tier) > 1:
            listed = "; ".join(f"{item['name']}: {run_command(item)}" for item in tier)
            raise errors.AutonomError(
                errors.AMBIGUOUS_TARGET,
                f"{len(tier)} targets could run the tour; pick one with --target or --avd",
                f"Candidates: {listed}",
                candidates=tier)
        return tier[0]
    return None


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
    return {
        "available": True,
        "platform": choice["platform"],
        "device": choice,
        "app_id": flow.app_id or built_in["app_id"],
        "title": built_in["title"] if flow_override is None else flow.name,
        "flow": str(flow_path),
        "steps": flow_labels(flow_path),
        "evidence": "a screenshot and the UI hierarchy after every step; the device log "
                    "where a step produced log lines",
        "run_command": run_command(choice),
    }


# --- the walk ------------------------------------------------------------------


def _boot(choice: dict[str, Any], args: argparse.Namespace) -> tuple[Target, dict[str, Any]]:
    detail: dict[str, Any] = {"booted_by_tour": False}
    if choice["platform"] == ANDROID:
        adb_path = adb_mod.find_adb(getattr(args, "adb", None))
        if choice.get("boot_needed"):
            if not choice.get("avd"):
                raise errors.AutonomError(
                    errors.NO_TARGET,
                    f"target {choice.get('target_id')} is not running and has no AVD to boot",
                    "Bring it online (see 'autonom devices'), or pass --avd <name>.")
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
    if run.get("report_html"):
        lines.append(f"- HTML report: `{run['report_html']}`  (open it in a browser)")
        lines.append(f"- JUnit: `{run['report_junit']}`, bundle: `{run['report_bundle']}`")
    elif run.get("report_error"):
        lines.append(f"- no report could be built: {run['report_error']}")
    lines.append(f"- journal of every verb: `{run['journal']}`")
    if run.get("failure"):
        lines.append("")
        lines.append("## The step that failed")
        lines.append("")
        failure = run["failure"]
        where = f"`{failure.get('command')}` at line {failure.get('line')}" \
            if failure.get("command") else f"`{failure.get('error_code')}`"
        lines.append(f"{where}: {failure.get('error')}")
        if run.get("repair"):
            lines.append("")
            lines.append("Repair brief:")
            for command in run["repair"].get("commands", []):
                lines.append(f"- `{command}`")
    lines.append("")
    lines.append("## Next")
    lines.append("")
    lines.append(f"- `autonom report open --session {run['session_id']} --run {run['run_id']}`"
                 " — the interactive report")
    lines.append(f"- `{run['journal']}` — the session journal: every action, in order")
    lines.append("- write your own flow: `autonom flow create --from-session <id>` after a manual session")
    if run.get("booted_by_tour") and not run.get("shutdown"):
        lines.append(f"- the device is still up; `autonom devices shutdown --target {run['target_id']}` powers it off")
    return "\n".join(lines) + "\n"


def _idb_probe(args: argparse.Namespace) -> dict[str, Any]:
    """`ios_idb.probe`, but never an OS error: an explicit idb path that does
    not exist surfaces from the probe as a bare FileNotFoundError."""
    try:
        return ios_idb.probe(getattr(args, "idb", None), None)
    except (OSError, errors.AutonomError) as exc:
        return {"state": "missing", "error": str(exc)}


def ios_hid_readiness(idb: dict[str, Any], target: Target | None = None) -> dict[str, Any]:
    """Can the iOS walk tap? Pure over the idb probe snapshot and the HID
    backend selection `ui_ios` uses (AUTONOM_IOS_HID, AUTONOM_AXE, PATH).

    The accessibility tree always comes from idb, so ``idb`` must be ready;
    taps go through whichever HID backend `ui_ios` would pick — idb when its
    HID works (the probe's ``hid.ready``; unknown counts as working, exactly
    as `ui_ios` treats it), AXe when it is installed and selected or idb's
    HID is known broken. ``ready`` is False only when neither can tap.
    A configured remote companion (AUTONOM_IDB_COMPANION or the target's
    ``idb_companion`` alias) counts as HID-ready whatever the local probe
    says, exactly as `ui_ios` never probes it.
    """
    from . import ui_ios

    mode = ui_ios.hid_mode(target)
    axe = ui_ios.find_axe(target)
    idb_ready = idb.get("state") == "ready"
    idb_hid = idb_ready and (bool(ios_idb.target_companion(target))
                             or bool((idb.get("hid") or {}).get("ready", True)))
    if mode == "idb":
        backend = "idb" if idb_hid else None
    elif mode == "axe":
        backend = "axe" if axe else None
    else:
        backend = "idb" if idb_hid else ("axe" if axe else None)
    return {"ready": idb_ready and backend is not None, "tree": idb_ready,
            "backend": backend, "mode": mode, "axe": axe,
            "idb_hid_ready": idb_hid}


def _refuse_unready_ios(idb: dict[str, Any], hid: dict[str, Any]) -> None:
    """The typed refusal for an iOS walk that cannot read or cannot tap."""
    from . import ui_ios

    if not hid["tree"]:
        raise errors.AutonomError(
            errors.IDB_REQUIRED,
            "the iOS walk reads the screen through idb's accessibility tree, "
            "and idb is not ready",
            "Install idb (brew install idb-companion; pipx install fb-idb) or run the "
            "tour on Android: autonom tour --run --platform android.",
            idb=idb, hid=hid)
    if hid["mode"] == "axe":
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{ui_ios.HID_ENV}=axe but no axe binary was found",
            ui_ios.AXE_INSTALL_HINT, idb=idb, hid=hid)
    if hid.get("axe"):
        # Only reachable with AUTONOM_IOS_HID=idb: in auto mode an installed
        # AXe would have been picked, so saying it is missing would send the
        # operator to install something they already have.
        raise errors.AutonomError(
            errors.IOS_HID_FRAMEWORK_MISSING,
            "the iOS walk taps through idb's HID input, which cannot load "
            "SimulatorKit on this Xcode; AXe is installed but not selected "
            f"because {ui_ios.HID_ENV}=idb forces idb",
            f"Select AXe with {ui_ios.HID_ENV}=axe (or unset {ui_ios.HID_ENV} so "
            "auto picks it), or upgrade idb-companion.",
            fix=(idb.get("hid") or {}).get("fix") or ios_idb.HID_UPGRADE_FIX,
            idb=idb, hid=hid)
    raise errors.AutonomError(
        errors.IOS_HID_FRAMEWORK_MISSING,
        "the iOS walk taps through idb's HID input, which cannot load "
        "SimulatorKit on this Xcode, and AXe is not installed",
        ios_idb.HID_FRAMEWORK_HINT,
        fix=(idb.get("hid") or {}).get("fix") or ios_idb.HID_UPGRADE_FIX,
        idb=idb, hid=hid)


def _refuse_active_session() -> None:
    """The tour owns a session of its own; starting it would overwrite the
    current-session pointer and orphan a user's session with its proxy, log
    streams and recorder."""
    try:
        current = session_mod.load_current()
    except (OSError, ValueError):
        current = None  # an unreadable pointer owns nothing a new session could orphan
    if current:
        raise errors.AutonomError(
            errors.SESSION_ALREADY_ACTIVE,
            f"session {current.get('session_id')} is active; the tour would replace it",
            "Finish it first with 'autonom session stop', then run the tour again.",
            session_id=current.get("session_id"),
            target_id=current.get("target_id"))


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


def _summarize(*, choice: dict[str, Any], target: Target, record: dict[str, Any],
               title: str, app_id: str, run_id: str, status: str,
               step_rows: list[dict[str, Any]], failure: dict[str, Any] | None,
               flow_path: Path, events_path: str | None,
               boot_detail: dict[str, Any], started: float) -> dict[str, Any]:
    """Reports and the step-by-step account for one recorded run — the same
    for a run that finished and one an infrastructure failure aborted."""
    from .flow import repair as flow_repair

    artifacts_dir = Path(record["artifacts_dir"])
    run_dir = session_mod.artifact_path(record, "flows", run_id)
    shots_dir = artifacts_dir / "shots" / run_id
    summary: dict[str, Any] = {
        "status": status,
        "title": title,
        "platform": target.platform,
        "target_id": target.target_id,
        "device": {**choice, "target_id": target.target_id},
        "app_id": app_id,
        "session_id": record["session_id"],
        "artifacts_dir": str(artifacts_dir),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "shots_dir": str(shots_dir),
        "journal": str(artifacts_dir / "journal.ndjson"),
        **boot_detail,
    }
    html_path = run_dir / "report.html"
    junit_path = run_dir / "report.xml"
    try:
        manifest = flow_report.load_manifest(run_dir)
        html_path.write_text(flow_report.render_html(manifest, artifacts_dir), encoding="utf-8")
        _private(html_path)
        junit_path.write_text(flow_report.render_junit(manifest), encoding="utf-8")
        _private(junit_path)
        bundle = report_bundle_mod.build(manifest, artifacts_root=artifacts_dir,
                                         out=run_dir / "bundle-v2")
        summary.update({"report_html": str(html_path), "report_junit": str(junit_path),
                        "report_bundle": bundle["bundle"]})
        if not step_rows:
            step_rows = list(manifest.get("steps") or [])
        failure = failure or manifest.get("primary_error")
    except errors.AutonomError as exc:
        # No manifest means no report; the account below still says what ran.
        summary["report_error"] = exc.message

    steps = []
    for outcome in step_rows:
        entry = {key: outcome.get(key) for key in
                 ("index", "command", "label", "status", "duration_ms", "error", "error_code")}
        if entry["index"] is not None:
            entry.update(_evidence_for(run_dir, shots_dir, entry["index"]))
        steps.append({k: v for k, v in entry.items() if v is not None})
    summary["steps"] = steps
    if failure:
        summary["failure"] = failure
        if events_path:
            # The executed timeline exactly as `flow run` hands it over —
            # status, hook and selector included — so the brief's
            # `--until-step` skips recovered retry attempts and cleanup hooks
            # instead of guessing from indices alone.
            timeline = [{k: v for k, v in outcome.items() if v is not None}
                        for outcome in step_rows]
            brief = flow_repair.repair_brief(str(flow_path), failure, timeline,
                                             events_path=events_path)
            if brief:
                summary["repair"] = brief
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    return summary


def _write_account(summary: dict[str, Any]) -> None:
    text = narrative(summary)
    tour_md = Path(summary["run_dir"]) / "tour.md"
    tour_md.parent.mkdir(parents=True, exist_ok=True)
    tour_md.write_text(text, encoding="utf-8")
    _private(tour_md)
    summary["tour_md"] = str(tour_md)
    summary["narrative"] = text


def run(choice: dict[str, Any], args: argparse.Namespace, *,
        flow_override: Path | None = None, shutdown: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    _refuse_active_session()
    built_in = BUILT_IN[choice["platform"]]
    flow_path = flow_override or built_in["flow"]
    flow = flow_validator.validate_tree(flow_path)
    app_id = flow.app_id or built_in["app_id"]
    title = built_in["title"] if flow_override is None else flow.name

    tooling: dict[str, Any] = {}
    if choice["platform"] == IOS:
        # Checked before anything boots: a simulator the tour booted only to
        # find it cannot tap is exactly the mess the tour must not leave.
        tooling["idb"] = _idb_probe(args)
        tooling["hid"] = ios_hid_readiness(tooling["idb"])
        if not tooling["hid"]["ready"]:
            _refuse_unready_ios(tooling["idb"], tooling["hid"])

    try:
        target, boot_detail = _boot(choice, args)
    except BaseException:
        # A simulator boot that failed or was interrupted part-way may have
        # left the device up; with --shutdown it goes back to how it was.
        if shutdown and choice["platform"] == IOS and choice.get("boot_needed"):
            try:
                ios_simctl.shutdown(ios_simctl.find_simctl(getattr(args, "simctl", None)),
                                    choice["target_id"])
            except errors.AutonomError:
                pass
        raise
    record: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    aborted: errors.AutonomError | None = None
    try:
        if target.platform == IOS:
            tooling["simctl"] = target.tool
        else:
            tooling["adb"] = target.tool
        record = session_mod.start_session(
            target.tool, serial=target.serial, app_id=app_id,
            platform=target.platform, target_id=target.target_id, tooling=tooling)

        config = flow_executor.RunConfig(
            evidence_mode="always", evidence_collect=("screenshot", "hierarchy", "logs"))
        runner = flow_executor.Executor(target, record, config)
        common = {"choice": choice, "target": target, "record": record, "title": title,
                  "app_id": app_id, "flow_path": flow_path, "boot_detail": boot_detail,
                  "started": started}
        try:
            result = runner.run(flow)
        except errors.AutonomError as exc:
            # An infrastructure failure (the first real run met a hung
            # `uiautomator dump`): the executor still wrote the manifest, so
            # the account and the reports of what happened are written too,
            # and the envelope says where they are.
            exc.extra.update({"session_id": record["session_id"],
                              "artifacts_dir": record["artifacts_dir"]})
            aborted = exc
            run_id = getattr(runner, "_writer_run_id", None)
            if run_id:
                try:
                    summary = _summarize(run_id=run_id, status="failed", step_rows=[],
                                         failure={"error_code": exc.code, "error": exc.message},
                                         events_path=None, **common)
                except (errors.AutonomError, OSError, ValueError):
                    summary = None  # the original failure is the one to report
                if summary is not None:
                    exc.extra.update({"run_id": run_id, "run_dir": summary["run_dir"]})
                    if summary.get("report_html"):
                        exc.extra["report_html"] = summary["report_html"]
            raise
        summary = _summarize(run_id=result.run_id, status=result.status,
                             step_rows=[vars(outcome) for outcome in result.steps],
                             failure=result.failure, events_path=result.events_path,
                             **common)
    finally:
        # Whatever ended the walk — a flow failure, an infrastructure error,
        # an exception nobody expected, Ctrl-C — the tour's own session is
        # closed and a device it booted is powered off when asked.
        stopped = False
        if record is not None:
            try:
                current = session_mod.load_current()
            except (OSError, ValueError):
                current = None
            if current and current.get("session_id") == record["session_id"]:
                session_mod.stop_session()
                stopped = True
        powered_off = False
        if shutdown and boot_detail.get("booted_by_tour"):
            try:
                if target.platform == ANDROID:
                    emulator_mod.kill_emulator(target.tool, target.target_id)
                else:
                    ios_simctl.shutdown(target.tool, target.target_id)
                powered_off = True
            except errors.AutonomError:
                powered_off = False
        if summary is not None:
            summary["session_stopped"] = stopped
            if powered_off:
                summary["shutdown"] = True
            if aborted is not None:
                # The walk failed: the account of what happened is the
                # evidence a human reads first, so it is written anyway.
                try:
                    _write_account(summary)
                    aborted.extra["tour_md"] = summary["tour_md"]
                except OSError:
                    pass

    _write_account(summary)
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
                     f"and walk {prop['title']} — a screenshot and the UI hierarchy after each "
                     f"step, the device log where a step produced log lines — then hand you the "
                     f"session directory and a report.")
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
        if platform and device["platform"] != platform:
            raise errors.AutonomError(
                errors.CONFLICTING_TARGET_FLAGS,
                f"{explicit} is an {device['platform']} target, but --platform is {platform}",
                "Drop --platform, or pick a target of that platform from 'autonom devices'.")
        avd = getattr(args, "avd", None)
        if avd and device.get("avd") != avd:
            raise errors.AutonomError(
                errors.CONFLICTING_TARGET_FLAGS,
                f"--target {explicit} and --avd {avd} name different devices",
                "Pass one of them.")
        if not device.get("running") and device["platform"] == ANDROID:
            # An attached Android device that is offline or unauthorized has
            # no AVD the tour could boot; it can only be reported.
            raise errors.AutonomError(
                errors.NO_TARGET,
                f"target {explicit} is {device.get('state') or 'not running'}, not ready",
                "Bring it online (see 'autonom devices'), or let the tour boot an AVD "
                "with --avd <name>.",
                state=device.get("state"))
        choice = {"platform": device["platform"], "target_id": explicit,
                  "name": device.get("avd") or device.get("name"),
                  "boot_needed": not device.get("running")}
    else:
        choice = choose(inv, platform, getattr(args, "avd", None),
                        strict=bool(getattr(args, "run", False)))
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
        wants_run = answer in ("y", "yes")
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
