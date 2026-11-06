"""Config, state paths, CLI resolution and the child environment (contract 2.3).

Everything here takes the environment and the home directory explicitly, and
``Resolver`` takes injectable ``which`` and ``run`` callables, so tests never
depend on the developer's real tools. Python 3.9 compatible, stdlib only.
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

from mcp_common import PathScrubber, ToolError, rejected

MIN_CLI_PYTHON = (3, 11)
PLUGIN_DIR = Path(__file__).resolve().parents[1]
STATE_DIR_NAME = "chatgpt-mcp"
CLAIM_ENV = ("AUTONOM_CONTROL_HOLDER", "AUTONOM_CONTROL_GENERATION")
COPY_LAUNCHER = re.compile(r"#\s*autonom launcher \(copy mode\)\s*->\s*(.+?)\s*$")
TARGET_SPEC = re.compile(r"^(android|ios):[A-Za-z0-9._:-]{1,128}$")
WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

# key -> (default, minimum, maximum, integer?)
NUMERIC_KEYS: Dict[str, Tuple[Any, Any, Any, bool]] = {
    "control_idle_s": (120, 15, 3600, True),
    "agent_lease_ttl_s": (120, 5, 3600, True),
    "max_streams": (4, 1, 8, True),
    "max_runs": (2, 1, 8, True),
    "frame_budget_bytes": (524288, 65536, 1048576, True),
    "stream_buffer_bytes": (16777216, 1048576, 67108864, True),
    "stream_idle_s": (30, 5, 300, True),
    "inline_image_max_bytes": (1048576, 0, 2097152, True),
    "run_max_s": (1800, 60, 3600, True),
    "cli_timeout_scale": (1.0, 0.01, 10, False),
}
PATH_KEYS = ("autonom_cli", "cli_python", "autonom_root")


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------- paths

def state_base(env: Mapping[str, str], home: Path) -> Path:
    """``$AUTONOM_HOME``, else ``$XDG_STATE_HOME/autonom``, else ``~/.local/state/autonom``."""
    explicit = env.get("AUTONOM_HOME")
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "autonom"
    return Path(home) / ".local" / "state" / "autonom"


def mcp_state_dir(env: Mapping[str, str], home: Path) -> Path:
    return state_base(env, home) / STATE_DIR_NAME


def sessions_home(env: Mapping[str, str], home: Path) -> Path:
    """The CLI's session store: ``$AUTONOM_HOME/sessions``, else ``~/.autonom/sessions``."""
    explicit = env.get("AUTONOM_HOME")
    base = Path(explicit) if explicit else Path(home) / ".autonom"
    return base / "sessions"


def local_path_labels(env: Mapping[str, str], home: Path) -> List[Tuple[str, str]]:
    """``(prefix, label)`` pairs for the local paths no tool result may show.

    The state base becomes ``<state>``, ``$XDG_STATE_HOME`` ``<xdg-state>``,
    ``$AUTONOM_HOME`` ``<autonom-home>`` and the home ``~``; each also in its
    resolved form (``/var`` is ``/private/var`` on macOS). The session store
    lies under one of them.
    """
    pairs: List[Tuple[str, str]] = []
    items = [(str(state_base(env, home)), "<state>"), (env.get("AUTONOM_HOME"), "<autonom-home>"),
             (env.get("XDG_STATE_HOME"), "<xdg-state>"), (str(home), "~")]
    for raw, label in items:
        if not raw:
            continue
        pairs.append((str(raw), label))
        try:
            resolved = os.path.realpath(str(raw))
        except (OSError, ValueError):
            continue
        if resolved != str(raw):
            pairs.append((resolved, label))
    return pairs


def path_scrubber(env: Mapping[str, str], home: Path) -> PathScrubber:
    return PathScrubber(local_path_labels(env, home))


def ensure_private_dir(path: Path) -> Path:
    """Make ``path`` a directory only its owner can use, and return it.

    Every missing component, parents included, is created 0700 (``os.makedirs``
    applies its mode to the leaf only, so each level is made one by one). An
    existing ``path`` that grants group or other access is tightened to its
    owner bits; existing parents are left as they are.
    """
    path = Path(path)
    missing: List[Path] = []
    probe = path
    while not probe.is_dir():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for item in reversed(missing):
        try:
            os.mkdir(str(item), 0o700)
        except FileExistsError:
            if not item.is_dir():
                raise
            continue  # a concurrent creator won; its mode is checked below for the leaf
        try:
            os.chmod(str(item), 0o700)  # the umask may have masked owner bits
        except OSError:
            pass
    try:
        mode = os.stat(str(path)).st_mode & 0o777
        if mode & 0o077:
            os.chmod(str(path), mode & 0o700)
    except OSError:
        pass
    return path


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


def child_env(env: Mapping[str, str], home: Path,
              claim: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """The server's environment plus the augmented PATH, minus the claim
    variables, which are set only from ``claim`` (``holder``, ``generation``)."""
    out = dict(env)
    out["PATH"] = augmented_path(env, home)
    for key in CLAIM_ENV:
        out.pop(key, None)
    if claim:
        out["AUTONOM_CONTROL_HOLDER"] = str(claim["holder"])
        out["AUTONOM_CONTROL_GENERATION"] = str(claim["generation"])
    return out


# ---------------------------------------------------------------- config

class Config(dict):
    """Validated config values plus ``warnings`` (``config_error`` entries)."""

    def __init__(self, values: Dict[str, Any], warnings: List[Dict[str, str]]) -> None:
        super().__init__(values)
        self.warnings = warnings


def default_values() -> Dict[str, Any]:
    values: Dict[str, Any] = {key: None for key in PATH_KEYS}
    values.update({"flow_roots": [], "allowed_targets": [], "canvas_workspace": "chatgpt"})
    for key, (default, _low, _high, _integer) in NUMERIC_KEYS.items():
        values[key] = default
    return values


def load_config(env: Mapping[str, str], home: Path) -> Config:
    """Read ``<MCP state dir>/config.json``. A bad value never crashes the
    server: that key keeps its default and a ``config_error`` warning names
    the key (never the value). Unknown keys are kept and ignored."""
    values = default_values()
    warnings: List[Dict[str, str]] = []

    def problem(message: str) -> None:
        warnings.append({"code": "config_error", "message": message[:500]})

    path = mcp_state_dir(env, home) / "config.json"
    raw: Any = {}
    try:
        text: Optional[str] = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        text = None
    except (OSError, UnicodeDecodeError):
        text = None
        problem("config.json could not be read")
    if text is not None:
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError):
            raw = {}
            problem("config.json is not valid JSON")
    if not isinstance(raw, dict):
        problem("config.json must hold a JSON object")
        raw = {}

    for key in PATH_KEYS:
        if key in raw and raw[key] is not None:
            value = raw[key]
            if isinstance(value, str) and value and os.path.isabs(value) and "\n" not in value:
                values[key] = value
            else:
                problem("%s must be an absolute path" % key)

    if "flow_roots" in raw:
        roots = raw["flow_roots"]
        if not isinstance(roots, list) or len(roots) > 8 or not all(
                isinstance(item, str) and item and os.path.isabs(item) for item in roots):
            problem("flow_roots must be a list of at most 8 absolute directories")
        else:
            kept: List[str] = []
            for item in roots:
                if item not in kept:
                    kept.append(item)
            values["flow_roots"] = kept

    if "allowed_targets" in raw:
        targets = raw["allowed_targets"]
        if not isinstance(targets, list) or len(targets) > 32 or not all(
                isinstance(item, str) and TARGET_SPEC.fullmatch(item) for item in targets):
            problem("allowed_targets must be a list of at most 32 android:<id> or ios:<udid> entries")
        else:
            values["allowed_targets"] = sorted(set(targets), key=targets.index)

    if "canvas_workspace" in raw:
        name = raw["canvas_workspace"]
        if isinstance(name, str) and WORKSPACE.fullmatch(name):
            values["canvas_workspace"] = name
        else:
            problem("canvas_workspace must match ^[A-Za-z0-9._-]{1,40}$")

    for key, (_default, low, high, integer) in NUMERIC_KEYS.items():
        if key not in raw:
            continue
        value = raw[key]
        valid = is_int(value) if integer else is_number(value)
        if valid and low <= value <= high:
            values[key] = value if integer else float(value)
        else:
            problem("%s must be a%s %s..%s" % (key, "n integer" if integer else " number", low, high))
    return Config(values, warnings)


# ---------------------------------------------------------------- resolution

WhichFn = Callable[[str, str], Optional[str]]
RunFn = Callable[[Sequence[str], float, Mapping[str, str]], Tuple[int, str]]


def default_which(name: str, path: str) -> Optional[str]:
    return shutil.which(name, path=path)


def default_run(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> Tuple[int, str]:
    """Run ``argv`` without a shell; (exit code, stdout). Never raises."""
    try:
        proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, env=dict(env),
                              check=False, close_fds=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _version_tuple(text: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"^\s*(\d+)\.(\d+)\s*$", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


class Resolver:
    """Finds the CLI script, its interpreter, the Autonom root and the UI files."""

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
        self._cli: Optional[str] = None
        self._cli_python: Optional[Tuple[str, str]] = None

    def _explicit(self, env_key: str, config_key: str) -> Tuple[Optional[str], Optional[str]]:
        value = self.env.get(env_key)
        if value:
            return value, "environment"
        value = self.config.get(config_key)
        if value:
            return str(value), "config"
        return None, None

    @staticmethod
    def _cli_script(candidate: str) -> Optional[str]:
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
        """Absolute path of ``autonom.py``; ``cli_unavailable`` (rejected) otherwise."""
        if self._cli is not None:
            return self._cli
        explicit, source = self._explicit("AUTONOM_CLI", "autonom_cli")
        if explicit is not None:
            script = self._cli_script(explicit)
            if not script:
                raise rejected("cli_unavailable",
                               "The Autonom CLI set in the adapter %s is not an Autonom CLI or does "
                               "not exist" % source)
            self._cli = script
            return script
        candidates = [str(self.home / ".local" / "bin" / "autonom")]
        found = self.which("autonom", self.path)
        if found:
            candidates.append(found)
        for candidate in candidates:
            script = self._cli_script(candidate)
            if script:
                self._cli = script
                return script
        raise rejected("cli_unavailable", "The Autonom CLI was not found")

    def _probe_python(self, candidate: str) -> Optional[str]:
        code, out = self.run([candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             5.0, child_env(self.env, self.home))
        if code != 0 or not out.strip():
            return None
        version = _version_tuple(out.strip().splitlines()[-1])
        return None if version is None else "%d.%d" % version

    def resolve_cli_python(self) -> str:
        """An interpreter >= 3.11 for the CLI; ``cli_unavailable`` (rejected) otherwise."""
        if self._cli_python is not None:
            return self._cli_python[0]
        explicit, source = self._explicit("AUTONOM_MCP_CLI_PYTHON", "cli_python")
        if explicit is not None:
            version = self._probe_python(explicit) if os.path.isfile(explicit) else None
            parsed = _version_tuple(version or "")
            if parsed is None or parsed < MIN_CLI_PYTHON:
                raise rejected("cli_unavailable",
                               "The interpreter set in the adapter %s is not Python 3.11 or newer"
                               % source,
                               "Set cli_python (or AUTONOM_MCP_CLI_PYTHON) to Python 3.11 or newer.")
            self._cli_python = (explicit, version or "")
            return explicit
        if self.version_info[:2] >= MIN_CLI_PYTHON and self.executable:
            self._cli_python = (self.executable, "%d.%d" % tuple(self.version_info[:2]))
            return self.executable
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
            parsed = _version_tuple(version or "")
            if parsed is not None and parsed >= MIN_CLI_PYTHON:
                self._cli_python = (candidate, version or "")
                return candidate
        raise rejected("cli_unavailable", "No Python 3.11 or newer was found for the Autonom CLI",
                       "Install Python 3.11 or newer, or set cli_python in the adapter config.")

    def resolve_autonom_root(self) -> str:
        """The checkout or install the CLI lives in (``autonom_root`` wins)."""
        explicit = self.config.get("autonom_root")
        if explicit:
            return str(explicit)
        cli = self.resolve_cli()
        parents = Path(cli).parents
        return str(parents[1] if len(parents) > 1 else Path(cli).parent)

    def ui_dir(self) -> Path:
        explicit = self.env.get("AUTONOM_MCP_UI_DIR")
        if explicit:
            return Path(explicit)
        return PLUGIN_DIR / "ui"


def tool_error_reason(exc: ToolError) -> str:
    return "%s: %s" % (exc.error_code, exc.error)
