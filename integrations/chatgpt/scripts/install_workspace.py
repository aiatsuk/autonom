#!/usr/bin/env python3
"""Install, uninstall or inspect the Autonom MCP adapter dev plugin.

Contract section 7. The plugin ``integrations/chatgpt/autonom-chatgpt`` is
copied (never symlinked: worktrees disappear) to ``<home>/plugins/autonom-chatgpt``
and one entry is merged into the personal marketplace
``<home>/.agents/plugins/marketplace.json``. Every write of that marketplace is
preceded by an exact byte backup under ``<home>/plugins/backups/autonom-chatgpt/``.
The adapter config lives in ``<state>/chatgpt-mcp/config.json`` (``AUTONOM_HOME``,
else ``$XDG_STATE_HOME/autonom``, else ``<home>/.local/state/autonom``).

The script never writes under ``<home>/.codex``, except ``uninstall
--purge-cache``, which removes the adapter's own plugin cache directory after
checking that every manifest inside it names the adapter. It never edits
``~/.codex/config.toml``; ``status`` only reads the plugin table headers of that
file and the names of the plugin cache folders (the ChatGPT inventory).

Generic helpers come from ``install_probe.py`` (same folder, unchanged); the
config validators come from the adapter's own ``server/mcp_env.py``.

Usage:
  install_workspace.py install   [--home DIR] [--dry-run] [--json]
                                 [--flow-root DIR]... [--allow-target SPEC]...
                                 [--canvas-workspace NAME] [--control-idle N]
                                 [--autonom-cli FILE]
  install_workspace.py uninstall [--home DIR] [--dry-run] [--json] [--purge-cache]
  install_workspace.py status    [--home DIR] [--json]

Exit codes: 0 success, 1 refusal (JSON payload with error_code, error, hint),
2 usage error (bad flags or flag values).
"""
from __future__ import annotations

import sys

# No bytecode next to the helpers or inside the plugin tree that gets copied.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

PLUGIN_NAME = "autonom-chatgpt"
DISPLAY_NAME = "Autonom (dev)"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_SOURCE = SCRIPT_DIR.parent / PLUGIN_NAME
ENTRY_PATH = "./plugins/" + PLUGIN_NAME
STAMP_NAME = ".autonom-chatgpt-install.json"
STAMP_SCHEMA = 1
STATE_DIR_NAME = "chatgpt-mcp"
DEFAULT_MARKETPLACE_NAME = "personal"
DEFAULT_MARKETPLACE_MODE = 0o644
MAX_FLOW_ROOTS = 8
MAX_TARGETS = 32
MAX_CODEX_CONFIG_BYTES = 4 * 1024 * 1024
# Test-only: install from a copy of the plugin tree instead of the repository's.
SOURCE_ENV = "AUTONOM_MCP_INSTALL_SOURCE"

NEXT_STEPS = ("Restart ChatGPT desktop, open Plugins, choose the Personal marketplace, "
              "install Autonom (dev), enable it in Work mode.")
MANUAL_STEPS = ("Uninstall Autonom (dev) in ChatGPT desktop (Plugins). If ~/.codex/config.toml "
                "still holds a [plugins.\"autonom-chatgpt@<marketplace>\"] table, copy the file to "
                "~/plugins/backups/autonom-chatgpt/ and remove that one table by hand; this script "
                "never edits that file.")

ENTRY: dict[str, Any] = {
    "name": PLUGIN_NAME,
    "source": {"source": "local", "path": ENTRY_PATH},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
}

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
try:
    import install_probe as probe_helpers  # generic helpers only; that file is never changed
except ImportError:  # a copied script without its neighbours: refused in main()
    probe_helpers = None  # type: ignore[assignment]

# The adapter's validators and state paths are the single source of truth.
sys.path.insert(0, str(REPO_SOURCE / "server"))
try:
    import mcp_env  # noqa: E402
except ImportError:  # a copied script without its plugin tree: refused in main()
    mcp_env = None  # type: ignore[assignment]

HINTS = {
    "invalid_arguments": "Fix the named flag and run the command again.",
    "home_invalid": "Pass an existing directory with --home.",
    "source_invalid": ("Run the script from an Autonom checkout that holds "
                       "integrations/chatgpt/autonom-chatgpt and integrations/chatgpt/scripts/"
                       "install_probe.py."),
    "marketplace_invalid": ("Fix ~/.agents/plugins/marketplace.json by hand (it must be a JSON object "
                            "with a plugins list), then run the command again."),
    "entry_conflict": ("Another marketplace entry is named autonom-chatgpt; rename or remove it "
                       "by hand, then run the command again."),
    "target_conflict": ("~/plugins/autonom-chatgpt was not created by this script; move it "
                        "away by hand, then run install again."),
    "config_invalid": ("Fix or delete the adapter config file (it must hold a JSON object), then run "
                       "install again."),
    "cache_conflict": ("The cache directory holds a plugin that is not the adapter; remove it by hand "
                       "or run uninstall without --purge-cache."),
    "write_failed": "Check the permissions of the named path and run the command again.",
}

# A TOML table header ``[plugins."<name>"]`` (basic or literal quoted key).
PLUGIN_TABLE = re.compile(
    r"""^\s*\[\s*plugins\s*\.\s*(?:"((?:[^"\\\n]|\\.)*)"|'([^'\n]*)')\s*\]\s*(?:#.*)?$""")


class Refusal(Exception):
    """A refused command: one JSON payload, exit 1 (or 2 for usage errors)."""

    def __init__(self, error_code: str, error: str, *, exit_code: int = 1, **extras: Any) -> None:
        super().__init__(error)
        self.error_code = error_code
        self.error = error
        self.exit_code = exit_code
        self.extras = extras

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error_code": self.error_code, "error": self.error,
                               "hint": HINTS.get(self.error_code, "")}
        out.update(self.extras)
        return out


# ---------------------------------------------------------------- small helpers

def tree_hashes(root: Path) -> dict[str, str]:
    """``{relative posix path: sha256}`` minus caches and our stamp.

    Raises ``UnreadableFile`` (an ``OSError``) for a file that cannot be read.
    """
    hashes = probe_helpers.tree_hashes(root)
    hashes.pop(STAMP_NAME, None)
    return hashes


# ---------------------------------------------------------------- layout

class Layout:
    """Every path the script reads or writes, derived from ``home`` and the env."""

    def __init__(self, home: Path, env: dict[str, str], source: Path) -> None:
        self.home = home
        self.source = source
        self.plugins_dir = home / "plugins"
        self.copy = self.plugins_dir / PLUGIN_NAME
        self.stamp = self.copy / STAMP_NAME
        self.marketplace = home / ".agents" / "plugins" / "marketplace.json"
        self.backup_dir = self.plugins_dir / "backups" / PLUGIN_NAME
        self.config = Path(os.path.abspath(mcp_env.mcp_state_dir(env, home) / "config.json"))
        self.codex_config = home / ".codex" / "config.toml"
        self.cache_root = home / ".codex" / "plugins" / "cache"

    def cache_dir(self, marketplace_name: str) -> Path:
        return self.cache_root / marketplace_name / PLUGIN_NAME


def resolve_home(raw: str | None) -> Path:
    home = Path(raw).expanduser() if raw else Path.home()
    home = Path(os.path.abspath(home))
    if not home.is_dir():
        raise Refusal("home_invalid", "The home directory does not exist or is not a directory",
                      path=str(home))
    return home


def source_path(env: dict[str, str]) -> Path:
    raw = env.get(SOURCE_ENV)
    return Path(os.path.abspath(raw)) if raw else REPO_SOURCE


def resolve_source(env: dict[str, str]) -> tuple[Path, str]:
    """The plugin tree to install and its manifest version."""
    source = source_path(env)
    manifest = source / ".codex-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise Refusal("source_invalid", "The adapter plugin source has no readable manifest",
                      path=str(source)) from None
    if not isinstance(data, dict) or data.get("name") != PLUGIN_NAME or not isinstance(
            data.get("version"), str):
        raise Refusal("source_invalid", "The adapter plugin source manifest is not the adapter's",
                      path=str(source))
    return source, data["version"]


# ---------------------------------------------------------------- marketplace

def read_marketplace(path: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    """(raw bytes, parsed object) or (None, None) when the file does not exist."""
    try:
        return probe_helpers.read_marketplace(path)
    except probe_helpers.Refusal as exc:  # the helper's generic checks, our payload
        raise Refusal(exc.error_code, exc.error, **exc.extras) from None


def named_entries(data: dict[str, Any]) -> list[int]:
    return [index for index, item in enumerate(data["plugins"])
            if isinstance(item, dict) and item.get("name") == PLUGIN_NAME]


def entry_is_ours(item: dict[str, Any]) -> bool:
    source = item.get("source")
    return isinstance(source, dict) and source.get("path") == ENTRY_PATH


def check_entries(data: dict[str, Any] | None, path: Path) -> list[int]:
    """Indexes of our entries; refuses when an entry with our name is foreign."""
    if data is None:
        return []
    indexes = named_entries(data)
    for index in indexes:
        if not entry_is_ours(data["plugins"][index]):
            source = data["plugins"][index].get("source")
            found = source.get("path") if isinstance(source, dict) else None
            raise Refusal("entry_conflict",
                          "A marketplace entry named autonom-chatgpt points elsewhere",
                          path=str(path), found_path=found if isinstance(found, str) else None)
    return indexes


# ---------------------------------------------------------------- copy and stamp

def read_stamp(layout: Layout) -> dict[str, Any] | None:
    """The stamp when the copy is a real directory holding the adapter's stamp."""
    if layout.copy.is_symlink() or not layout.copy.is_dir():
        return None
    try:
        data = json.loads(layout.stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("plugin") != PLUGIN_NAME:
        return None
    return data


def check_target(layout: Layout) -> dict[str, Any] | None:
    """Our previous stamp, None when there is no copy; refuses a foreign one."""
    if not os.path.lexists(layout.copy):
        return None
    stamp = read_stamp(layout)
    if stamp is None:
        raise Refusal("target_conflict",
                      "%s exists and was not installed by this script" % layout.copy,
                      path=str(layout.copy))
    return stamp


def copy_tree(layout: Layout, stamp_fields: dict[str, Any]) -> None:
    """Copy the source next to the target, stamp it, then swap it into place."""
    layout.plugins_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    tmp = layout.plugins_dir / (".%s.tmp-%d" % (PLUGIN_NAME, pid))
    old = layout.plugins_dir / (".%s.old-%d" % (PLUGIN_NAME, pid))
    for leftover in (tmp, old):
        if os.path.lexists(leftover):
            shutil.rmtree(leftover)
    try:
        shutil.copytree(layout.source, tmp, symlinks=False, ignore=probe_helpers.copy_ignore)
        stamp = dict(stamp_fields)
        stamp["files"] = tree_hashes(tmp)
        probe_helpers.atomic_write(tmp / STAMP_NAME, probe_helpers.dump_json(stamp), 0o644)
        if os.path.lexists(layout.copy):
            os.rename(layout.copy, old)
        os.rename(tmp, layout.copy)
    except BaseException:
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        if os.path.lexists(old) and not os.path.lexists(layout.copy):
            os.rename(old, layout.copy)
        raise
    if os.path.lexists(old):
        shutil.rmtree(old)


# ---------------------------------------------------------------- config

def read_config(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise Refusal("config_invalid", "The adapter config file is not readable JSON",
                      path=str(path)) from None
    if not isinstance(data, dict):
        raise Refusal("config_invalid", "The adapter config file is not a JSON object",
                      path=str(path))
    return data


def write_config(path: Path, data: dict[str, Any]) -> None:
    """Write the config 0600; a state dir made here is 0700 (contract 0)."""
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(path.parent, 0o700)
    probe_helpers.atomic_write(path, probe_helpers.dump_json(data), 0o600)


def config_path_is_ours(raw: Any) -> bool:
    """Uninstall only deletes a path that looks like the adapter config."""
    if not isinstance(raw, str) or not os.path.isabs(raw):
        return False
    path = Path(raw)
    return path.name == "config.json" and path.parent.name == STATE_DIR_NAME


def bad_flag(flag: str, problem: str) -> Refusal:
    return Refusal("invalid_arguments", "%s: %s" % (flag, problem), exit_code=2,
                   field=flag, problem=problem)


def config_updates(args: argparse.Namespace) -> dict[str, Any]:
    """Validated config keys named by flags (contract 2.3 ranges, section 7 rules)."""
    updates: dict[str, Any] = {}
    if args.flow_root:
        roots: list[str] = []
        for raw in args.flow_root:
            if not raw or not os.path.isabs(raw) or "\n" in raw:
                raise bad_flag("--flow-root", "must be an absolute directory path")
            path = os.path.normpath(raw)
            if not os.path.isdir(path):
                raise bad_flag("--flow-root", "must be an existing directory")
            if path not in roots:
                roots.append(path)
        if len(roots) > MAX_FLOW_ROOTS:
            raise bad_flag("--flow-root", "at most %d directories" % MAX_FLOW_ROOTS)
        updates["flow_roots"] = roots
    if args.allow_target:
        targets: list[str] = []
        for spec in args.allow_target:
            if not mcp_env.TARGET_SPEC.fullmatch(spec):
                raise bad_flag("--allow-target", "must be android:<id> or ios:<udid>")
            if spec not in targets:
                targets.append(spec)
        if len(targets) > MAX_TARGETS:
            raise bad_flag("--allow-target", "at most %d targets" % MAX_TARGETS)
        updates["allowed_targets"] = targets
    if args.canvas_workspace is not None:
        if not mcp_env.WORKSPACE.fullmatch(args.canvas_workspace):
            raise bad_flag("--canvas-workspace", "must match ^[A-Za-z0-9._-]{1,40}$")
        updates["canvas_workspace"] = args.canvas_workspace
    if args.control_idle is not None:
        _default, low, high, _integer = mcp_env.NUMERIC_KEYS["control_idle_s"]
        if not low <= args.control_idle <= high:
            raise bad_flag("--control-idle", "must be an integer %d-%d" % (low, high))
        updates["control_idle_s"] = args.control_idle
    if args.autonom_cli is not None:
        raw = args.autonom_cli
        if not raw or not os.path.isabs(raw) or "\n" in raw:
            raise bad_flag("--autonom-cli", "must be an absolute path")
        path = os.path.normpath(raw)
        if os.path.basename(path) != "autonom.py" or \
                os.path.basename(os.path.dirname(path)) != "scripts":
            raise bad_flag("--autonom-cli", "must name a scripts/autonom.py file")
        if not os.path.isfile(path):
            raise bad_flag("--autonom-cli", "must be an existing file")
        updates["autonom_cli"] = path
    return updates


# ---------------------------------------------------------------- ChatGPT inventory (read-only)

def config_plugin_tables(path: Path) -> tuple[list[str], list[str]]:
    """Sorted ``<name>@<marketplace>`` keys of the ``[plugins."..."]`` headers.

    Only header lines are matched; nothing else in the file reaches the output.
    """
    if not os.path.lexists(path):
        return [], []
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CODEX_CONFIG_BYTES + 1)
    except OSError as exc:
        return [], ["%s cannot be read (%s)" % (path, type(exc).__name__)]
    problems: list[str] = []
    if len(raw) > MAX_CODEX_CONFIG_BYTES:
        raw = raw[:MAX_CODEX_CONFIG_BYTES]
        problems.append("%s is larger than %d bytes; only the start was scanned"
                        % (path, MAX_CODEX_CONFIG_BYTES))
    names: set[str] = set()
    for line in raw.decode("utf-8", "replace").splitlines():
        match = PLUGIN_TABLE.match(line)
        if not match:
            continue
        name = match.group(1) if match.group(1) is not None else match.group(2)
        if "@" in name:
            names.add(name)
    return sorted(names), problems


def printable(name: str) -> str:
    """A folder name as valid UTF-8 text (undecodable bytes become U+FFFD)."""
    return os.fsencode(name).decode("utf-8", "replace")


def cache_plugin_dirs(cache_root: Path) -> tuple[list[str], list[str]]:
    """Sorted ``<plugin>@<marketplace>`` of ``<cache>/<marketplace>/<plugin>/`` folders."""
    if not os.path.isdir(cache_root):  # never raises, unlike Path.is_dir on some versions
        return [], []
    names: set[str] = set()
    problems: list[str] = []
    try:
        markets = sorted(p for p in cache_root.iterdir() if os.path.isdir(p))
    except OSError as exc:
        return [], ["%s cannot be listed (%s)" % (cache_root, type(exc).__name__)]
    for market in markets:
        try:
            plugins = [p for p in market.iterdir() if os.path.isdir(p)]
        except OSError as exc:
            problems.append("%s cannot be listed (%s)" % (printable(str(market)), type(exc).__name__))
            continue
        for plugin in plugins:
            names.add("%s@%s" % (printable(plugin.name), printable(market.name)))
    return sorted(names), problems


def chatgpt_inventory(layout: Layout) -> dict[str, Any]:
    config_plugins, problems = config_plugin_tables(layout.codex_config)
    cache_plugins, cache_problems = cache_plugin_dirs(layout.cache_root)
    return {"config_plugins": config_plugins, "cache_plugins": cache_plugins,
            "problems": problems + cache_problems}


# ---------------------------------------------------------------- commands

def cmd_install(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    updates = config_updates(args)
    home = resolve_home(args.home)
    source, version = resolve_source(env)
    layout = Layout(home, env, source)

    raw_market, market = read_marketplace(layout.marketplace)
    indexes = check_entries(market, layout.marketplace)
    previous = check_target(layout)
    existing_config = read_config(layout.config)

    actions: list[dict[str, Any]] = []

    # Marketplace: add or replace our entry in place; others keep order and values.
    if market is None:
        new_market: dict[str, Any] = {"name": DEFAULT_MARKETPLACE_NAME,
                                      "interface": {"displayName": "Personal"},
                                      "plugins": [copy.deepcopy(ENTRY)]}
        market_changed, created_marketplace = True, True
    else:
        new_market = copy.deepcopy(market)
        plugins = new_market["plugins"]
        if indexes:
            plugins[indexes[0]] = copy.deepcopy(ENTRY)
            for index in reversed(indexes[1:]):
                del plugins[index]
        else:
            plugins.append(copy.deepcopy(ENTRY))
        market_changed, created_marketplace = new_market != market, False

    # Config: keys named by flags replace, everything else is kept.
    if existing_config is None:
        new_config = dict(updates)
        config_changed, created_config = True, True
    else:
        new_config = dict(existing_config)
        new_config.update(updates)
        config_changed, created_config = new_config != existing_config, False

    # Copy: refresh unless the existing copy already equals the source.
    try:
        source_files = tree_hashes(source)
    except probe_helpers.UnreadableFile as exc:
        raise Refusal("source_invalid",
                      "A file in the adapter plugin source cannot be read (%s)" % exc.reason,
                      path=exc.path) from None
    copy_needed = True
    if previous is not None:
        try:
            copy_files = tree_hashes(layout.copy)
        except probe_helpers.UnreadableFile as exc:
            raise Refusal("write_failed", "A file in the installed copy cannot be read (%s)" % exc.reason,
                          path=exc.path) from None
        copy_needed = not (previous.get("files") == copy_files == source_files)

    prev = previous or {}
    prev_backup = prev.get("first_backup") if isinstance(prev.get("first_backup"), str) else None
    backup_needed = market_changed and raw_market is not None
    planned_backup = (probe_helpers.next_backup_path(layout, probe_helpers.utc_now())
                      if backup_needed else None)
    wrote_config = created_config or (prev.get("wrote_config") is True
                                      and prev.get("config_path") == str(layout.config))
    stamp_fields: dict[str, Any] = {
        "schema": STAMP_SCHEMA,
        "plugin": PLUGIN_NAME,
        "version": version,
        "source": str(source),
        "source_commit": None,
        "installed_at": None,
        "files": {},
        "created_marketplace": created_marketplace or prev.get("created_marketplace") is True,
        "wrote_config": wrote_config,
        "config_path": str(layout.config),
        "first_backup": prev_backup,
    }
    stamp_keys = ("created_marketplace", "wrote_config", "config_path")
    stamp_needed = (not copy_needed and previous is not None and (
        any(previous.get(key) != stamp_fields[key] for key in stamp_keys)
        or (prev_backup is None and backup_needed)))

    if copy_needed:
        actions.append({"op": "copy", "path": str(layout.copy), "from": str(source)})
    if backup_needed:
        actions.append({"op": "backup", "path": str(planned_backup),
                        "from": str(layout.marketplace)})
    if market_changed:
        actions.append({"op": "write_marketplace", "path": str(layout.marketplace)})
    if config_changed:
        actions.append({"op": "write_config", "path": str(layout.config),
                        "keys": sorted(updates)})
    if stamp_needed:
        actions.append({"op": "write_stamp", "path": str(layout.stamp)})

    result: dict[str, Any] = {
        "ok": True, "command": "install", "dry_run": bool(args.dry_run),
        "changed": bool(actions), "actions": actions,
        "copy": str(layout.copy), "marketplace": str(layout.marketplace),
        "config": str(layout.config), "next_steps": NEXT_STEPS,
    }
    if args.dry_run:
        return result

    current = ""
    try:
        if backup_needed:
            current = str(layout.backup_dir)
            made = probe_helpers.write_backup(layout, raw_market or b"")
            for action in actions:
                if action["op"] == "backup":
                    action["path"] = str(made)
            stamp_fields["first_backup"] = prev_backup or str(made)
        if copy_needed:
            current = str(layout.copy)
            stamp_fields["installed_at"] = probe_helpers.iso_z(probe_helpers.utc_now())
            stamp_fields["source_commit"] = probe_helpers.git_head(source)
            copy_tree(layout, stamp_fields)
        elif stamp_needed:
            current = str(layout.stamp)
            refreshed = dict(previous or {})
            for key in stamp_keys + ("first_backup",):
                refreshed[key] = stamp_fields[key]
            probe_helpers.atomic_write(layout.stamp, probe_helpers.dump_json(refreshed), 0o644)
        if market_changed:
            current = str(layout.marketplace)
            probe_helpers.write_marketplace(
                layout.marketplace, new_market,
                probe_helpers.file_mode(layout.marketplace) if raw_market is not None
                else DEFAULT_MARKETPLACE_MODE)
        if config_changed:
            current = str(layout.config)
            write_config(layout.config, new_config)
    except OSError as exc:
        raise Refusal("write_failed", "Writing failed (%s)" % type(exc).__name__,
                      path=current, actions=actions) from None
    return result


def cache_check(cache_dir: Path) -> bool:
    """True when the cache dir exists and is the adapter's; refuses a foreign one."""
    if not os.path.lexists(cache_dir):
        return False
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        raise Refusal("cache_conflict", "The adapter cache path is not a plain directory",
                      path=str(cache_dir))
    manifests: list[Path] = []
    has_files = False
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        if filenames:
            has_files = True
        if Path(dirpath).name == ".codex-plugin" and "plugin.json" in filenames:
            manifests.append(Path(dirpath) / "plugin.json")
    if has_files and not manifests:
        raise Refusal("cache_conflict", "The adapter cache directory holds files but no plugin manifest",
                      path=str(cache_dir))
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            data = None
        if not isinstance(data, dict) or data.get("name") != PLUGIN_NAME:
            raise Refusal("cache_conflict", "A plugin manifest in the adapter cache is not the adapter's",
                          path=str(manifest))
    return True


def cmd_uninstall(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    home = resolve_home(args.home)
    layout = Layout(home, env, REPO_SOURCE)

    raw_market, market = read_marketplace(layout.marketplace)
    indexes = check_entries(market, layout.marketplace)
    stamp = read_stamp(layout)
    cache_dir = layout.cache_dir(probe_helpers.marketplace_name(market))
    purge = bool(args.purge_cache) and cache_check(cache_dir)

    actions: list[dict[str, Any]] = []
    kept: list[dict[str, str]] = []

    new_market: dict[str, Any] | None = None
    delete_market = False
    if market is not None and indexes:
        new_market = copy.deepcopy(market)
        for index in reversed(indexes):
            del new_market["plugins"][index]
        delete_market = bool(stamp and stamp.get("created_marketplace") is True
                             and not new_market["plugins"])
        actions.append({"op": "backup",
                        "path": str(probe_helpers.next_backup_path(layout, probe_helpers.utc_now())),
                        "from": str(layout.marketplace)})
        actions.append({"op": "remove" if delete_market else "write_marketplace",
                        "path": str(layout.marketplace)})

    remove_copy = stamp is not None
    if remove_copy:
        actions.append({"op": "remove", "path": str(layout.copy)})
    elif os.path.lexists(layout.copy):
        kept.append({"path": str(layout.copy), "reason": "not installed by this script"})

    config_target: Path | None = None
    if stamp and stamp.get("wrote_config") is True:
        raw_path = stamp.get("config_path")
        if config_path_is_ours(raw_path):
            if os.path.lexists(raw_path):
                config_target = Path(raw_path)
                actions.append({"op": "remove", "path": raw_path})
        else:
            kept.append({"path": str(raw_path), "reason": "stamp config_path is not an adapter config"})
    elif os.path.lexists(layout.config):
        kept.append({"path": str(layout.config), "reason": "config not created by install"})

    if purge:
        actions.append({"op": "remove", "path": str(cache_dir)})

    result: dict[str, Any] = {
        "ok": True, "command": "uninstall", "dry_run": bool(args.dry_run),
        "changed": bool(actions), "actions": actions, "kept": kept,
        "manual_steps": MANUAL_STEPS,
    }
    if args.dry_run:
        return result

    current = ""
    try:
        if new_market is not None:
            current = str(layout.backup_dir)
            made = probe_helpers.write_backup(layout, raw_market or b"")
            actions[0]["path"] = str(made)
            current = str(layout.marketplace)
            if delete_market:
                os.unlink(layout.marketplace)
            else:
                probe_helpers.write_marketplace(layout.marketplace, new_market,
                                                probe_helpers.file_mode(layout.marketplace))
        if remove_copy:
            current = str(layout.copy)
            shutil.rmtree(layout.copy)
        if config_target is not None:
            current = str(config_target)
            os.unlink(config_target)
        if purge:
            current = str(cache_dir)
            shutil.rmtree(cache_dir)
    except OSError as exc:
        raise Refusal("write_failed", "Removing failed (%s)" % type(exc).__name__,
                      path=current, actions=actions) from None
    return result


def cmd_status(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    home = resolve_home(args.home)
    source = source_path(env)
    layout = Layout(home, env, source)

    market_info: dict[str, Any] = {"path": str(layout.marketplace),
                                   "exists": os.path.lexists(layout.marketplace),
                                   "valid": False, "name": None, "plugins": []}
    market: dict[str, Any] | None = None
    entry: dict[str, Any] | None = None
    try:
        _, market = read_marketplace(layout.marketplace)
    except Refusal as exc:
        market_info["error_code"] = exc.error_code
        market_info["error"] = exc.error
    if market is not None:
        market_info["valid"] = True
        market_info["name"] = market.get("name") if isinstance(market.get("name"), str) else None
        market_info["plugins"] = probe_helpers.plugin_names(market)
        indexes = named_entries(market)
        if indexes:
            entry = market["plugins"][indexes[0]]

    stamp = read_stamp(layout)
    copy_info: dict[str, Any] = {"path": str(layout.copy), "exists": os.path.lexists(layout.copy),
                                 "stamp_ok": False, "files_match_source": False, "version": None}
    if stamp is not None:
        copy_info["version"] = stamp.get("version")
        copy_info["installed_at"] = stamp.get("installed_at")
        try:
            copy_files: dict[str, str] | None = tree_hashes(layout.copy)
        except probe_helpers.UnreadableFile as exc:
            copy_files = None
            copy_info["reason"] = "unreadable file in the copy: %s (%s)" % (exc.path, exc.reason)
        copy_info["stamp_ok"] = (copy_files is not None and stamp.get("schema") == STAMP_SCHEMA
                                 and stamp.get("files") == copy_files)
        try:
            source_files = tree_hashes(source) if source.is_dir() else None
        except probe_helpers.UnreadableFile as exc:
            source_files = None
            copy_info["source_reason"] = "unreadable file in the source: %s (%s)" % (exc.path, exc.reason)
        copy_info["files_match_source"] = (copy_files is not None and source_files is not None
                                           and copy_files == source_files)

    cache_dir = layout.cache_dir(probe_helpers.marketplace_name(market))
    versions: list[str] = []
    if cache_dir.is_dir() and not cache_dir.is_symlink():
        try:
            versions = sorted(p.name for p in cache_dir.iterdir() if p.is_dir())
        except OSError:
            versions = []
    cache_info = {"present": os.path.lexists(cache_dir), "path": str(cache_dir), "versions": versions}

    config_info: dict[str, Any] = {"path": str(layout.config),
                                   "exists": os.path.lexists(layout.config),
                                   "valid": None, "values": None}
    if config_info["exists"]:
        try:
            config_info["values"] = read_config(layout.config)
            config_info["valid"] = True
        except Refusal:
            config_info["valid"] = False

    return {
        "ok": True, "command": "status",
        "installed": entry is not None and entry_is_ours(entry) and bool(copy_info["stamp_ok"]),
        "entry": entry, "copy": copy_info, "marketplace": market_info, "cache": cache_info,
        "config": config_info, "backups": probe_helpers.list_backups(layout), "source": str(source),
        "chatgpt": chatgpt_inventory(layout),
    }


# ---------------------------------------------------------------- output and main

def human(result: dict[str, Any]) -> str:
    command = result.get("command")
    lines: list[str] = []
    if command == "status":
        lines.append("%s installed: %s" % (DISPLAY_NAME, "yes" if result["installed"] else "no"))
        copy_info = result["copy"]
        lines.append("Copy: %s (stamp %s, matches source: %s)" % (
            copy_info["path"], "ok" if copy_info["stamp_ok"] else "missing or stale",
            "yes" if copy_info["files_match_source"] else "no"))
        market = result["marketplace"]
        lines.append("Marketplace: %s (plugins: %s)" % (
            market["path"], ", ".join(market["plugins"]) or "none"))
        lines.append("Cache: %s (%s)" % (result["cache"]["path"],
                                         "present" if result["cache"]["present"] else "absent"))
        lines.append("Config: %s (%s)" % (result["config"]["path"],
                                          "present" if result["config"]["exists"] else "absent"))
        lines.append("Backups: %d" % len(result["backups"]))
        inventory = result["chatgpt"]
        lines.append("ChatGPT config plugins: %s" % (", ".join(inventory["config_plugins"]) or "none"))
        lines.append("ChatGPT cache plugins: %s" % (", ".join(inventory["cache_plugins"]) or "none"))
        for problem in inventory["problems"]:
            lines.append("ChatGPT inventory: %s" % problem)
        return "\n".join(lines)
    prefix = "Planned" if result.get("dry_run") else "Done"
    if not result["actions"]:
        lines.append("Nothing to change.")
    for action in result["actions"]:
        lines.append("%s: %s %s" % (prefix, action["op"], action["path"]))
    for item in result.get("kept", []):
        lines.append("Kept: %s (%s)" % (item["path"], item["reason"]))
    if command == "install":
        lines.append("Next: " + NEXT_STEPS)
    elif command == "uninstall":
        lines.append("Manual: " + result["manual_steps"])
    return "\n".join(lines)


class JsonArgumentParser(argparse.ArgumentParser):
    """Usage errors as one JSON refusal (exit 2) when ``--json`` is on the command line."""

    json_errors = False

    def error(self, message: str) -> Any:
        if JsonArgumentParser.json_errors:
            self.print_usage(sys.stderr)
            payload = Refusal("invalid_arguments", message, exit_code=2).payload()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            raise SystemExit(2)
        return super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="install_workspace.py",
        description="Install, uninstall or inspect the Autonom MCP adapter dev plugin.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, dry_run: bool = True) -> None:
        p.add_argument("--home", help="home directory to work in (default: your home)")
        p.add_argument("--json", action="store_true", help="print one JSON object")
        if dry_run:
            p.add_argument("--dry-run", action="store_true", help="check and plan, write nothing")

    install = sub.add_parser("install", help="copy the adapter and add its marketplace entry")
    common(install)
    install.add_argument("--flow-root", action="append", default=[], metavar="DIR",
                         help="absolute directory of Flow v1 files (repeatable, at most 8)")
    install.add_argument("--allow-target", action="append", default=[], metavar="SPEC",
                         help="device the adapter may use: android:<id> or ios:<udid> (repeatable)")
    install.add_argument("--canvas-workspace", metavar="NAME",
                         help="workspace Canvas name (default chatgpt)")
    install.add_argument("--control-idle", type=int, metavar="N",
                         help="human control idle release in seconds (15-3600)")
    install.add_argument("--autonom-cli", metavar="FILE",
                         help="absolute path of the scripts/autonom.py the adapter runs")

    uninstall = sub.add_parser("uninstall", help="remove what install added")
    common(uninstall)
    uninstall.add_argument("--purge-cache", action="store_true",
                           help="also remove the adapter's own ChatGPT plugin cache directory")

    status = sub.add_parser("status", help="report what is installed and the ChatGPT inventory")
    common(status, dry_run=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    JsonArgumentParser.json_errors = "--json" in raw_argv
    args = build_parser().parse_args(raw_argv)
    env = dict(os.environ)
    handlers = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}
    try:
        if probe_helpers is None:
            raise Refusal("source_invalid", "install_probe.py next to this script is missing",
                          path=str(SCRIPT_DIR / "install_probe.py"))
        if mcp_env is None:
            raise Refusal("source_invalid", "The adapter plugin source next to this script is missing",
                          path=str(REPO_SOURCE))
        result = handlers[args.command](args, env)
    except Refusal as exc:
        payload = exc.payload()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print("error: %s (%s)" % (exc.error, exc.error_code), file=sys.stderr)
            if payload["hint"]:
                print("hint: %s" % payload["hint"], file=sys.stderr)
        return exc.exit_code
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
