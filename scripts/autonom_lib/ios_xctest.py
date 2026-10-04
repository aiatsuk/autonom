"""Optional, targetless XCUITest bridge. All device input remains inside Autonom.

The runner uses a simulator-container mailbox, not an exposed HTTP service.
Each mutation is submitted once. A lost response is an uncertain result, never
an invitation to repeat a tap on a second backend.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import time
import uuid

from . import errors, ios_simctl, processes, session
from .platform import Target

RUNNER_ID = "dev.autonom.ui.tests.xctrunner"
SOURCE = Path(__file__).resolve().parents[2] / "native" / "ios"
# A cold build compiles the runner against the selected Xcode; a warm one is
# a cache hit and returns at once.
BUILD_TIMEOUT = 600
# The first attach installs the runner on the simulator and launches it.
READY_TIMEOUT = 180
REQUEST_TIMEOUT = 30
STOP_TIMEOUT = 30
# Requests that change nothing on screen; a late answer to one is harmless.
READ_COMMANDS = frozenset({"snapshot", "geometry", "stop"})


def root(*, create: bool = True) -> Path:
    path = session.sessions_home().parent / "xcuitest"
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def lock(path: Path):
    import fcntl  # macOS only; importing Autonom on Windows/Linux needs no Xcode.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def available() -> bool:
    return bool(shutil.which("xcodebuild") and (SOURCE / "AutonomUI.xcodeproj").exists())


def state_dir(target: Target, *, create: bool = True) -> Path:
    # A UDID is supplied by target resolution; hash it before using it in a path.
    path = root(create=create) / hashlib.sha256(target.target_id.encode()).hexdigest()[:20]
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def _pid_alive(pid) -> bool:
    try:
        return session.pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def _alive(record: dict) -> bool:
    # Check nonce, not just a reusable PID. ready.json disappears on orderly exit.
    mailbox = record.get("mailbox")
    if not mailbox or not record.get("token"):
        return False
    if _read(Path(mailbox) / "ready.json").get("token") != record["token"]:
        return False
    return _pid_alive(record.get("pid"))


def _owner(target: Target) -> dict:
    """The session this simulator's runner belongs to: the selected session
    when it drives this simulator, else none (a command naming another
    simulator never claims or stops a runner for the current session)."""
    current = session.load_current() or {}
    return current if current.get("target_id") == target.target_id else {}


def _remove_bundles(directory: Path, *, keep: str | None = None) -> None:
    """Delete this simulator's runner result bundles: they hold element labels
    and are not evidence. Only called while no runner of this simulator is
    writing one, except the bundle of token `keep`."""
    for bundle in directory.glob("run-*.xcresult"):
        if keep and bundle.name == f"run-{keep}.xcresult":
            continue
        shutil.rmtree(bundle, ignore_errors=True)


def _remove_log(directory: Path) -> None:
    """Delete this simulator's runner log: it holds the XCTest console,
    typed text included. Only called while no runner of this simulator
    writes it (every runner of a simulator shares the one log file)."""
    (directory / "runner.log").unlink(missing_ok=True)


def _signature(runfile: Path) -> list[str]:
    """What `ps` must still show for a registry row to be this runner."""
    return ["xcodebuild", "test-without-building", str(runfile)]


def _build() -> Path:
    if not available():
        raise errors.AutonomError(errors.XCUITEST_UNAVAILABLE, "The XCUITest runner requires Xcode",
                                  "Select Xcode with xcode-select, or use --ui-backend idb.")
    version_result = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True,
                                    timeout=15, check=False)
    if version_result.returncode:
        raise errors.AutonomError(errors.XCUITEST_UNAVAILABLE, "A full Xcode installation is required",
                                  "Select Xcode with xcode-select before using the UI bridge.")
    version = version_result.stdout
    digest = hashlib.sha256(version.encode())
    for path in sorted(SOURCE.rglob("*")):
        if path.is_file() and path.suffix in {".swift", ".pbxproj", ".xcscheme"}:
            digest.update(path.read_bytes())
    build = root() / "builds" / digest.hexdigest()[:16]
    with lock(root() / "build.lock"):
        products = build / "Build" / "Products"
        runs = sorted(products.glob("*.xctestrun"))
        if runs and (build / "complete").exists():
            return runs[0]
        build.mkdir(parents=True, exist_ok=True)
        log = build / "build.log"
        try:
            with log.open("w") as stream:
                result = subprocess.run([
                    "xcodebuild", "-project", str(SOURCE / "AutonomUI.xcodeproj"),
                    "-scheme", "AutonomUI", "-sdk", "iphonesimulator",
                    "-destination", "generic/platform=iOS Simulator",
                    "-derivedDataPath", str(build), "build-for-testing",
                ], stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    timeout=BUILD_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as exc:
            raise errors.AutonomError(
                errors.XCUITEST_BUILD_FAILED,
                f"Building the XCUITest bridge took longer than {BUILD_TIMEOUT}s",
                f"Inspect {log}, then retry; a finished build is cached.", log=str(log)) from exc
        runs = sorted(products.glob("*.xctestrun"))
        if result.returncode or not runs:
            raise errors.AutonomError(errors.XCUITEST_BUILD_FAILED, "Could not build the XCUITest bridge",
                                      f"Inspect {log}", log=str(log))
        (build / "complete").touch()
        return runs[0]


def _test_entry(configuration: dict) -> dict:
    """The runner's test-target entry: run-file format 1 keys it by name at the
    top level, format 2 lists it under TestConfigurations[].TestTargets[]."""
    if isinstance(configuration.get("AutonomUITests"), dict):
        return configuration["AutonomUITests"]
    for config in configuration.get("TestConfigurations") or []:
        for test in config.get("TestTargets") or []:
            if isinstance(test, dict) and test.get("BlueprintName") == "AutonomUITests":
                return test
    raise errors.AutonomError(errors.XCUITEST_BUILD_FAILED,
                              "The built XCUITest run file has no AutonomUITests entry",
                              f"Delete the cached builds under {root() / 'builds'} and retry.")


def _ensure(target: Target, directory: Path, owner: dict | None = None) -> dict:
    """The live runner for this simulator, started when there is none.
    `owner` is the session record the request began with; it is read again
    from disk right before a new runner starts (the build takes time)."""
    state = _read(directory / "state.json")
    current = _owner(target)
    if _alive(state):
        if state.get("session_id") not in (None, current.get("session_id")):
            raise errors.AutonomError(errors.UI_BRIDGE_IN_USE,
                                      "Another session owns this simulator UI bridge",
                                      "Select its session ID or stop that session before attaching.",
                                      session_id=state["session_id"])
        return state
    # The previous runner ended (idle timeout, crash, failed start): its
    # bundles go before a new token starts. One still exiting keeps its own.
    old_token = state.get("token") if _pid_alive(state.get("pid")) else None
    _remove_bundles(directory, keep=old_token)
    template = _build()
    token = uuid.uuid4().hex
    configuration = plistlib.loads(template.read_bytes())

    # Keep __TESTROOT__ paths correct after placing the run file beside state.
    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value.replace("__TESTROOT__", str(template.parent)) if isinstance(value, str) else value

    configuration = expand(configuration)
    test = _test_entry(configuration)
    test.setdefault("EnvironmentVariables", {})["AUTONOM_RUNNER_TOKEN"] = token
    test["OnlyTestIdentifiers"] = ["BridgeTests/testServe"]
    test["SystemAttachmentLifetime"] = "deleteAlways"
    runfile = directory / "runner.xctestrun"
    runfile.write_bytes(plistlib.dumps(configuration))
    runfile.chmod(0o600)
    log = directory / "runner.log"
    # A `session stop` that began while the runner was being built must not
    # find a new runner after it returns: checked last, right before Popen,
    # still under request.lock.
    _refuse_stopped(session.on_disk(owner or current))
    # XCTest logs can contain element labels and typed values. Keep private and
    # never return log contents in a JSON error or journal.
    with log.open("w") as stream:
        log.chmod(0o600)
        child = subprocess.Popen([
            "xcodebuild", "test-without-building", "-xctestrun", str(runfile),
            "-destination", f"platform=iOS Simulator,id={target.target_id}",
            "-parallel-testing-enabled", "NO", "-maximum-concurrent-test-simulator-destinations", "1",
            "-resultBundlePath", str(directory / f"run-{token}.xcresult"),
        ], stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    # Registered under the owning session with a signature and its process
    # group, so `session stop` and `cleanup` can end it even when the mailbox
    # no longer answers, and never signal a reused pid.
    processes.register("xcuitest", child.pid, target_id=target.target_id,
                       session_id=current.get("session_id"),
                       artifacts_dir=current.get("artifacts_dir", str(directory)),
                       signature=_signature(runfile), process_group=child.pid)
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline and child.poll() is None:
        container = ios_simctl.run_simctl(target.tool, ["get_app_container", target.target_id,
                                                       RUNNER_ID, "data"], check=False, timeout=10)
        if container.returncode == 0:
            mailbox = Path(container.stdout.strip()) / "Documents" / "autonom-ui"
            if _read(mailbox / "ready.json").get("token") == token:
                state = {"pid": child.pid, "token": token, "mailbox": str(mailbox),
                         "target_id": target.target_id, "log": str(log),
                         "session_id": current.get("session_id")}
                _write(directory / "state.json", state)
                return state
        time.sleep(0.3)
    if child.poll() is None:
        processes.terminate_group(child.pid)
        child.wait(timeout=10)
    processes.deregister(child.pid)
    shutil.rmtree(directory / f"run-{token}.xcresult", ignore_errors=True)
    # The log may hold typed text from an earlier runner; it goes with the run.
    _remove_log(directory)
    raise errors.AutonomError(errors.XCUITEST_START_FAILED, "XCUITest bridge did not become ready",
                              "Check that the Simulator is booted and responsive, then retry, "
                              "or use --ui-backend idb.")


def _refuse_stopped(record: dict) -> None:
    """A session that is stopping or stopped (`session stop` in another
    terminal) must not get a new runner, nor send one input: `session stop`
    marks the session stopping before its teardown and could never reach a
    runner started after that. Any active stopper refuses: one stop that
    aborts leaves the mark of another that is still tearing down."""
    if record.get("stopped_at") or session.is_stopping(record):
        state = "has stopped" if record.get("stopped_at") else "is stopping"
        raise errors.AutonomError(
            errors.SESSION_STOPPED, f"session {record.get('session_id')} {state}",
            "Start a new session to drive the simulator.",
            session_id=record.get("session_id"))


def request(target: Target, command: str, *, app_id: str | None = None, **values) -> dict:
    current = session.load_current() or {}
    _refuse_stopped(session.on_disk(current))
    app_id = app_id or (current.get("app_id") if current.get("target_id") == target.target_id else None)
    if not app_id and command != "stop":
        raise errors.AutonomError(errors.APP_ID_REQUIRED, "XCUITest requires an explicit app session",
                                  "Start a session with --app-id and select it with --session-id.")
    directory = state_dir(target)
    with lock(directory / "request.lock"):
        # Again under the lock: the stop may begin while this waits for it.
        # Read from the session's own file, since a finished stop has already
        # removed the current pointer this command started from.
        _refuse_stopped(session.on_disk(current))
        state = _ensure(target, directory, current)
        return _exchange(state, command, app_id, values)


def _exchange(state: dict, command: str, app_id: str | None, values: dict) -> dict:
    """Send one request to the running runner `state` names and wait for its
    answer. The caller holds the simulator's request lock."""
    mailbox = Path(state["mailbox"])
    request_id = uuid.uuid4().hex
    payload = {"id": request_id, "token": state["token"], "command": command,
               "app_id": app_id, **values}
    _write(mailbox / "request.json", payload)
    deadline = time.monotonic() + REQUEST_TIMEOUT
    while time.monotonic() < deadline:
        response = _read(mailbox / "response.json")
        if response.get("id") == request_id and response.get("token") == state["token"]:
            (mailbox / "response.json").unlink(missing_ok=True)
            if not response.get("ok"):
                raise errors.AutonomError(response.get("error_code", errors.XCUITEST_FAILED),
                                          response.get("error", "XCUITest request failed"))
            return response.get("result", {})
        time.sleep(0.05)
    # Remove any unconsumed input, including text, before reporting failure.
    (mailbox / "request.json").unlink(missing_ok=True)
    code = errors.XCUITEST_TIMEOUT if command in READ_COMMANDS else errors.UI_ACTION_UNCERTAIN
    raise errors.AutonomError(code, "XCUITest did not acknowledge the request",
                              "Inspect the screen before another action; this request was not repeated.",
                              request_id=request_id)


def stop(target: Target) -> bool:
    """Stop this simulator's runner when the current session owns it.

    Asked through the mailbox first. A runner that does not answer, or has
    not exited within ``STOP_TIMEOUT``, is ended through its registry row:
    its process group, and only after `ps` shows the pid is still that
    runner. The row is dropped once the process is gone. Returns True when
    a runner process was stopped.
    """
    directory = state_dir(target, create=False)
    state = _read(directory / "state.json")
    if not state:
        # No tracked runner; a start that failed may have left a bundle.
        if directory.is_dir():
            with lock(directory / "request.lock"):
                if not (directory / "state.json").exists():
                    _remove_bundles(directory)
                    _remove_log(directory)
        return False
    current = _owner(target)
    if state.get("session_id") not in (None, current.get("session_id")):
        return False
    pid = state.get("pid")
    if _alive(state):
        # Sent straight to this runner, never through request(): a stop must
        # never build or start a runner. Liveness is checked again under the
        # lock, since a concurrent stop or the idle timeout may have ended it.
        with lock(directory / "request.lock"):
            live = _read(directory / "state.json")
            if live.get("token") == state.get("token") and _alive(live):
                app_id = current.get("app_id") if current.get("target_id") == target.target_id else None
                try:
                    _exchange(live, "stop", app_id, {})
                except errors.AutonomError:
                    pass  # the registry row below still ends it
    deadline = time.monotonic() + STOP_TIMEOUT
    while pid and _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    stopped = bool(pid)
    if pid and _pid_alive(pid):
        entry = next((item for item in processes.entries() if item.get("pid") == int(pid)), None)
        stopped = bool(entry) and processes.terminate_entry(entry) == "terminated"
    # Under the request lock: a concurrent request() may be inside _ensure
    # starting a new runner, and that runner's bundle and state.json stay.
    with lock(directory / "request.lock"):
        fresh = _read(directory / "state.json")
        newer = fresh.get("token") not in (None, state.get("token"))
        if pid and not _pid_alive(pid):
            processes.deregister(int(pid))
            # This run's bundle and any an earlier runner of this simulator
            # left, but never the bundle of a newer runner still alive.
            keep = fresh["token"] if newer and _pid_alive(fresh.get("pid")) else None
            _remove_bundles(directory, keep=keep)
            if keep is None:
                # A newer runner still alive writes the same log; it stays.
                _remove_log(directory)
        if not newer:
            (directory / "state.json").unlink(missing_ok=True)
    return stopped
