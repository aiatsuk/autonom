"""Environment resolution for the Autonom probe server (contract 2.3).

Everything here takes the environment, the home directory and, where a
process or a PATH lookup is involved, injectable ``which`` and ``run``
callables, so tests never depend on the developer's real tools.

Python 3.9 compatible: the server may run on macOS's ``/usr/bin/python3``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

PROBE_VERSION = "0.1.0"
SCHEMA_VERSION = "autonom.chatgpt-probe/v1"
DEFAULT_MEDIA_PORT = 47811
SANDBOX_ORIGIN = "https://web-sandbox.oaiusercontent.com"
DEFAULT_CLI_TIMEOUT = 15.0
MIN_CLI_PYTHON = (3, 11)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
WS_MODULE = Path("plugins/autonom/skills/android-emulator-browser/scripts/ws.mjs")

ANDROID_SPEC = re.compile(r"^android:[A-Za-z0-9._:-]{1,128}$")
IOS_SPEC = re.compile(r"^ios:[0-9A-Fa-f-]{36}$")
REFUSED_DEVICE = "android:emulator-5554"
TARGET_REF = re.compile(r"^(fixture|android|ios)~[A-Za-z0-9._:-]{1,128}$")
LOCAL_ROUTE = re.compile(r"^http://127\.0\.0\.1:([0-9]{1,5})$")
HTTPS_ORIGIN = re.compile(
    r"^https://[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*(:[0-9]{1,5})?$")
EXACT_ORIGIN = re.compile(
    r"^(https?)://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
    r"|\[[0-9a-f:]+\])(:[0-9]{1,5})?$")
WILDCARD_ORIGIN = re.compile(
    r"^https://\*\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+(:[0-9]{1,5})?$")
COPY_LAUNCHER = re.compile(r"#\s*autonom launcher \(copy mode\)\s*->\s*(.+?)\s*$")

PATH_KEYS = ("autonom_cli", "cli_python", "node", "autonom_root", "adb")

HINTS = {
    "invalid_arguments": "Fix the named argument and call again.",
    "autonom_cli_not_found": (
        "Install the Autonom CLI (./scripts/install_cli.sh in a checkout) or set autonom_cli "
        "in <state>/chatgpt-probe/config.json."),
    "python_too_old": "Install Python 3.11 or newer, or set cli_python in the probe config.",
    "cli_timeout": "Check `autonom doctor` in a terminal; raise cli_timeout_s if devices are slow.",
    "cli_failed": "Run the same autonom command in a terminal to see the full output.",
    "stale_revision": "Read the note again with probe_read and retry with its revision.",
    "stale_selection": "The selection changed in the probe page; read it again.",
    "node_not_found": "Install Node 22 or newer, or set node in the probe config (AUTONOM_PROBE_NODE).",
    "autonom_root_not_found": (
        "Set autonom_root in the probe config (or AUTONOM_ROOT) to an Autonom checkout or install."),
    "media_start_failed": "See reason; run the probe media endpoint by hand to debug.",
    "media_port_in_use": (
        "Another process holds the port; change media_port in the probe config and restart ChatGPT."),
    "media_not_running": "Open the probe again (open_workspace).",
    "device_not_allowed": "Add the device with install_probe.py --allow-device.",
    "probe_deliberate_failure": "This failure is intentional; the page checks how errors render.",
    "results_too_large": "Save a smaller results object (at most 512 KiB of compact JSON).",
    "results_invalid": "Send the results JSON of the probe page (schema_version 1, run_id, checks).",
    "results_contain_secret": "Redact the media token from the results before saving them.",
    "state_unwritable": "Check permissions of <state>/chatgpt-probe.",
}


class ProbeError(Exception):
    """A tool failure with an Autonom-style error payload."""

    def __init__(self, error_code: str, error: str, hint: Optional[str] = None,
                 **extras: Any) -> None:
        super().__init__(error)
        self.error_code = error_code
        self.error = error
        self.hint = hint if hint is not None else HINTS.get(error_code, "")
        self.extras = extras

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error_code": self.error_code, "error": self.error, "hint": self.hint}
        for key, value in self.extras.items():
            if key not in ("schema_version", "operation_id", "ok", "error_code", "error", "hint"):
                out[key] = value
        return out


WhichFn = Callable[[str, str], Optional[str]]
RunFn = Callable[[Sequence[str], float, Mapping[str, str]], Tuple[int, str]]


def default_which(name: str, path: str) -> Optional[str]:
    return shutil.which(name, path=path)


def default_run(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> Tuple[int, str]:
    """Run ``argv`` without a shell; return (exit code, stdout). Never raises."""
    try:
        proc = subprocess.run(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, env=dict(env), check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- state paths

def state_root(env: Mapping[str, str], home: Path) -> Path:
    """Same rule as ``processes.registry_dir()``: AUTONOM_HOME, XDG, home."""
    explicit = env.get("AUTONOM_HOME")
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "autonom"
    return Path(home) / ".local" / "state" / "autonom"


def state_dir(env: Mapping[str, str], home: Path) -> Path:
    """``<state>/chatgpt-probe``; created 0700 by the state store on first write."""
    return state_root(env, home) / "chatgpt-probe"


def config_path(env: Mapping[str, str], home: Path) -> Path:
    explicit = env.get("AUTONOM_PROBE_CONFIG")
    if explicit:
        return Path(explicit)
    return state_dir(env, home) / "config.json"


def augmented_path(env: Mapping[str, str], home: Path) -> str:
    """PATH plus the usual install locations, de-duplicated, order kept."""
    parts: List[str] = []
    for item in (env.get("PATH") or "").split(os.pathsep):
        if item and item not in parts:
            parts.append(item)
    for item in ("/opt/homebrew/bin", "/usr/local/bin", str(Path(home) / ".local" / "bin"),
                 "/usr/bin", "/bin"):
        if item not in parts:
            parts.append(item)
    return os.pathsep.join(parts)


def child_env(env: Mapping[str, str], home: Path) -> Dict[str, str]:
    out = dict(env)
    out["PATH"] = augmented_path(env, home)
    out.pop("AUTONOM_PROBE_TOKEN", None)
    return out


# ---------------------------------------------------------------- validators

def valid_device_spec(value: Any) -> bool:
    return isinstance(value, str) and bool(ANDROID_SPEC.fullmatch(value) or IOS_SPEC.fullmatch(value))


def device_allowed_spec(value: Any) -> bool:
    return valid_device_spec(value) and value != REFUSED_DEVICE


def _port_ok(text: Optional[str]) -> bool:
    if text is None:
        return True
    port = int(text)
    return 1 <= port <= 65535


def valid_route_origin(value: str) -> bool:
    match = LOCAL_ROUTE.fullmatch(value)
    if match:
        return _port_ok(match.group(1))
    if HTTPS_ORIGIN.fullmatch(value):
        _, _, rest = value.partition("://")
        port = rest.rpartition(":")[2] if ":" in rest else None
        return _port_ok(port)
    return False


def valid_allow_origin(value: str) -> bool:
    if WILDCARD_ORIGIN.fullmatch(value):
        return True
    match = EXACT_ORIGIN.fullmatch(value)
    if not match:
        return False
    port = match.group(6)
    return _port_ok(port[1:] if port else None)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------- config

def default_config() -> Dict[str, Any]:
    return {
        "media_port": DEFAULT_MEDIA_PORT,
        "remote_routes": [],
        "allow_origins": [SANDBOX_ORIGIN],
        "allowed_devices": [],
        "autonom_cli": None,
        "cli_python": None,
        "node": None,
        "autonom_root": None,
        "adb": None,
        "cli_timeout_s": DEFAULT_CLI_TIMEOUT,
        "config_error": None,
    }


def load_config(env: Mapping[str, str], home: Path) -> Dict[str, Any]:
    """Read the probe config; bad input becomes defaults plus ``config_error``.

    A bad item inside a list is dropped (and named in ``config_error``) rather
    than discarding the whole key, so one typo never disables the probe.
    ``media_port`` 0 (an ephemeral port) is accepted for tests and the harness.
    """
    config = default_config()
    problems: List[str] = []
    path = config_path(env, home)
    raw: Any = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        text = None
    except (OSError, UnicodeDecodeError) as exc:
        text = None
        problems.append("config file unreadable: %s" % type(exc).__name__)
    if text is not None:
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError):
            raw = {}
            problems.append("config file is not valid JSON")
    if not isinstance(raw, dict):
        problems.append("config file must hold a JSON object")
        raw = {}

    if "media_port" in raw:
        port = raw["media_port"]
        if is_int(port) and (port == 0 or 1024 <= port <= 65535):
            config["media_port"] = port
        else:
            problems.append("media_port must be an integer 1024-65535")

    if "remote_routes" in raw:
        routes = raw["remote_routes"]
        if not isinstance(routes, list):
            problems.append("remote_routes must be a list")
        else:
            kept: List[Dict[str, str]] = []
            for index, route in enumerate(routes):
                if len(kept) >= 4:
                    problems.append("remote_routes holds more than 4 entries; extra entries ignored")
                    break
                if not isinstance(route, dict):
                    problems.append("remote_routes[%d] must be an object" % index)
                    continue
                label, origin = route.get("label"), route.get("origin")
                if not isinstance(label, str) or not 1 <= len(label) <= 40:
                    problems.append("remote_routes[%d].label must be 1-40 characters" % index)
                    continue
                if not isinstance(origin, str) or not valid_route_origin(origin.lower()):
                    problems.append(
                        "remote_routes[%d].origin must be http://127.0.0.1:N or https://host[:port]" % index)
                    continue
                kept.append({"label": label, "origin": origin.lower()})
            config["remote_routes"] = kept

    if "allow_origins" in raw:
        origins = raw["allow_origins"]
        if not isinstance(origins, list):
            problems.append("allow_origins must be a list")
        else:
            for index, origin in enumerate(origins):
                if not isinstance(origin, str) or origin != origin.lower() or not valid_allow_origin(origin):
                    problems.append("allow_origins[%d] is not a lowercase origin or https://*.suffix" % index)
                    continue
                if origin not in config["allow_origins"]:
                    config["allow_origins"].append(origin)

    if "allowed_devices" in raw:
        devices = raw["allowed_devices"]
        if not isinstance(devices, list):
            problems.append("allowed_devices must be a list")
        else:
            kept_devices: List[str] = []
            for index, spec in enumerate(devices):
                if not valid_device_spec(spec):
                    problems.append("allowed_devices[%d] is not a device spec" % index)
                elif spec == REFUSED_DEVICE:
                    problems.append("allowed_devices[%d]: %s is never allowed" % (index, REFUSED_DEVICE))
                elif spec not in kept_devices:
                    kept_devices.append(spec)
            config["allowed_devices"] = kept_devices

    for key in PATH_KEYS:
        if key in raw and raw[key] is not None:
            value = raw[key]
            if isinstance(value, str) and value and os.path.isabs(value):
                config[key] = value
            else:
                problems.append("%s must be an absolute path" % key)

    if "cli_timeout_s" in raw:
        timeout = raw["cli_timeout_s"]
        if is_number(timeout) and 1 <= timeout <= 120:
            config["cli_timeout_s"] = float(timeout)
        else:
            problems.append("cli_timeout_s must be a number 1-120")

    override = env.get("AUTONOM_PROBE_CLI_TIMEOUT")
    if override:
        try:
            seconds = float(override)
        except ValueError:
            seconds = -1.0
        if 0 < seconds <= 600:
            config["cli_timeout_s"] = seconds
        else:
            problems.append("AUTONOM_PROBE_CLI_TIMEOUT must be a number of seconds")

    if problems:
        config["config_error"] = "; ".join(problems)[:1000]
    return config


# ---------------------------------------------------------------- resolvers

def _version_tuple(text: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"^\s*(\d+)\.(\d+)\s*$", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


class Resolver:
    """Finds the CLI, its interpreter, Node, the Autonom root and adb."""

    def __init__(self, env: Mapping[str, str], home: Path, config: Mapping[str, Any],
                 which: Optional[WhichFn] = None, run: Optional[RunFn] = None,
                 executable: Optional[str] = None,
                 version_info: Optional[Tuple[int, ...]] = None) -> None:
        self.env = dict(env)
        self.home = Path(home)
        self.config = dict(config)
        self.which = which or default_which
        self.run = run or default_run
        self.executable = executable if executable is not None else sys.executable
        self.version_info = tuple(version_info if version_info is not None else sys.version_info[:3])
        self.path = augmented_path(self.env, self.home)
        self._cli_python: Optional[Tuple[str, str]] = None

    def _explicit(self, env_key: str, config_key: str) -> Tuple[Optional[str], Optional[str]]:
        value = self.env.get(env_key)
        if value:
            return value, "env"
        value = self.config.get(config_key)
        if value:
            return str(value), "config"
        return None, None

    # -- CLI

    def _cli_script(self, candidate: str) -> Optional[str]:
        """The ``autonom.py`` a CLI candidate stands for, or None."""
        if not os.path.isfile(candidate):
            return None
        real = os.path.realpath(candidate)
        if real.endswith(".py"):
            return real
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as handle:
                lines = [handle.readline() for _ in range(5)]
        except OSError:
            return None
        for line in lines:
            match = COPY_LAUNCHER.search(line.rstrip("\n"))
            if match:
                target = match.group(1)
                if target.endswith(".py") and os.path.isfile(target):
                    return os.path.realpath(target)
        return None

    def resolve_cli(self) -> str:
        explicit, source = self._explicit("AUTONOM_CLI", "autonom_cli")
        if explicit is not None:
            script = self._cli_script(explicit)
            if script:
                return script
            extras: Dict[str, Any] = {"source": source}
            if os.path.isfile(explicit):
                extras["found"] = os.path.basename(explicit)
            raise ProbeError(
                "autonom_cli_not_found",
                "The Autonom CLI set in the probe %s is not an Autonom CLI or does not exist"
                % ("environment" if source == "env" else "config"),
                **extras)
        candidates = [str(self.home / ".local" / "bin" / "autonom")]
        found = self.which("autonom", self.path)
        if found:
            candidates.append(found)
        for candidate in candidates:
            if not os.path.isfile(candidate):
                continue
            script = self._cli_script(candidate)
            if script:
                return script
            raise ProbeError("autonom_cli_not_found",
                             "The autonom command found is not a launcher of autonom.py",
                             found=os.path.basename(candidate))
        raise ProbeError("autonom_cli_not_found", "The Autonom CLI was not found")

    # -- interpreter for the CLI

    def _probe_python(self, candidate: str) -> Optional[str]:
        code, out = self.run(
            [candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], 3.0,
            child_env(self.env, self.home))
        if code != 0:
            return None
        version = _version_tuple(out.strip().splitlines()[-1] if out.strip() else "")
        if version is None:
            return None
        return "%d.%d" % version

    def resolve_cli_python(self) -> Tuple[str, str]:
        """(interpreter path, "major.minor") of a Python >= 3.11 for the CLI."""
        if self._cli_python is not None:
            return self._cli_python
        if self.version_info[:2] >= MIN_CLI_PYTHON and self.executable:
            self._cli_python = (self.executable, "%d.%d" % tuple(self.version_info[:2]))
            return self._cli_python
        seen: List[str] = []
        explicit, source = self._explicit("AUTONOM_PROBE_CLI_PYTHON", "cli_python")
        if explicit is not None:
            version = self._probe_python(explicit) if os.path.isfile(explicit) else None
            if version and _version_tuple(version) >= MIN_CLI_PYTHON:
                self._cli_python = (explicit, version)
                return self._cli_python
            if version:
                seen.append(version)
            raise ProbeError(
                "python_too_old",
                "The interpreter set in the probe %s is not Python 3.11 or newer"
                % ("environment" if source == "env" else "config"),
                found=seen, source=source)
        candidates: List[str] = []
        for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
            hit = self.which(name, self.path)
            if hit:
                candidates.append(hit)
        candidates.extend(["/opt/homebrew/bin/python3", "/usr/local/bin/python3"])
        hit = self.which("python3", self.path)
        if hit:
            candidates.append(hit)
        tried: List[str] = []
        for candidate in candidates:
            if candidate in tried or not os.path.isfile(candidate):
                continue
            tried.append(candidate)
            version = self._probe_python(candidate)
            if not version:
                continue
            if _version_tuple(version) >= MIN_CLI_PYTHON:
                self._cli_python = (candidate, version)
                return self._cli_python
            if version not in seen:
                seen.append(version)
        raise ProbeError("python_too_old", "No Python 3.11 or newer was found for the Autonom CLI",
                         found=seen)

    # -- node, root, adb, files

    def resolve_node(self) -> str:
        explicit, source = self._explicit("AUTONOM_PROBE_NODE", "node")
        if explicit is not None:
            if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
                return explicit
            raise ProbeError("node_not_found", "The Node set in the probe %s does not exist"
                             % ("environment" if source == "env" else "config"), source=source)
        found = self.which("node", self.path)
        if found:
            return found
        raise ProbeError("node_not_found", "Node was not found on PATH")

    def resolve_autonom_root(self) -> str:
        explicit, source = self._explicit("AUTONOM_ROOT", "autonom_root")
        if explicit is not None:
            if (Path(explicit) / WS_MODULE).is_file():
                return os.path.realpath(explicit)
            raise ProbeError(
                "autonom_root_not_found",
                "The Autonom root set in the probe %s has no Canvas scripts"
                % ("environment" if source == "env" else "config"), source=source)
        try:
            cli = self.resolve_cli()
        except ProbeError as exc:
            raise ProbeError("autonom_root_not_found",
                             "The Autonom root could not be derived: %s" % exc.error)
        parents = Path(cli).parents
        root = parents[1] if len(parents) > 1 else Path(cli).parent
        if (root / WS_MODULE).is_file():
            return str(root)
        raise ProbeError("autonom_root_not_found",
                         "The Autonom CLI is not inside a checkout or install with the Canvas scripts")

    def resolve_adb(self) -> Optional[str]:
        candidates: List[str] = []
        if self.config.get("adb"):
            candidates.append(str(self.config["adb"]))
        for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            if self.env.get(key):
                candidates.append(str(Path(self.env[key]) / "platform-tools" / "adb"))
        candidates.append(str(self.home / "Library" / "Android" / "sdk" / "platform-tools" / "adb"))
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return self.which("adb", self.path)

    def media_script(self) -> str:
        explicit = self.env.get("AUTONOM_PROBE_MEDIA_SCRIPT")
        if explicit:
            return explicit
        return str(PLUGIN_DIR / "media" / "probe-media.mjs")

    def ui_dir(self) -> Path:
        explicit = self.env.get("AUTONOM_PROBE_UI_DIR")
        if explicit:
            return Path(explicit)
        return PLUGIN_DIR / "ui"
