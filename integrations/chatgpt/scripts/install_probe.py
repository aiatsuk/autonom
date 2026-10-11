#!/usr/bin/env python3
"""Install, uninstall or inspect the Autonom ChatGPT probe dev plugin.

Contract section 6. The probe is copied (never symlinked: worktrees disappear)
to ``<home>/plugins/autonom-chatgpt-probe`` and one entry is merged into the
personal marketplace ``<home>/.agents/plugins/marketplace.json``. Every write of
that marketplace is preceded by an exact byte backup under
``<home>/plugins/backups/autonom-chatgpt-probe/``. The probe config lives in
``<state>/chatgpt-probe/config.json`` (``AUTONOM_HOME``, else
``$XDG_STATE_HOME/autonom``, else ``<home>/.local/state/autonom``).

The script never writes under ``<home>/.codex``, except ``uninstall
--purge-cache``, which removes the probe's own plugin cache directory after
checking that every manifest inside it names the probe. It never edits
``~/.codex/config.toml``.

Usage:
  install_probe.py install   [--home DIR] [--dry-run] [--json] [--media-port N]
                             [--remote-route LABEL=ORIGIN]... [--allow-origin ORIGIN]...
                             [--allow-device SPEC]...
  install_probe.py uninstall [--home DIR] [--dry-run] [--json] [--purge-cache]
  install_probe.py status    [--home DIR] [--json]

Exit codes: 0 success, 1 refusal (JSON payload with error_code, error, hint),
2 usage error (bad flags or flag values).
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "autonom-chatgpt-probe"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_SOURCE = SCRIPT_DIR.parent / PLUGIN_NAME
ENTRY_PATH = "./plugins/" + PLUGIN_NAME
STAMP_NAME = ".autonom-probe-install.json"
STAMP_SCHEMA = 1
EXCLUDED_DIRS = frozenset({"__pycache__"})
EXCLUDED_FILES = frozenset({".DS_Store"})
EXCLUDED_SUFFIXES = (".pyc",)
DEFAULT_MARKETPLACE_NAME = "personal"
DEFAULT_MARKETPLACE_MODE = 0o644
MAX_REMOTE_ROUTES = 4
# Test-only: install from a copy of the plugin tree instead of the repository's.
SOURCE_ENV = "AUTONOM_PROBE_INSTALL_SOURCE"

NEXT_STEPS = ("Restart ChatGPT desktop, open Plugins, choose the Personal marketplace, "
              "install Autonom probe, enable it in Work mode.")

ENTRY: dict[str, Any] = {
    "name": PLUGIN_NAME,
    "source": {"source": "local", "path": ENTRY_PATH},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
}

# The server's validators are the single source of truth for config values.
# Import them without leaving bytecode in the plugin tree that gets copied.
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO_SOURCE / "server"))
try:
    import probe_env  # noqa: E402
except ImportError:  # a copied script without its plugin tree: refused in main()
    probe_env = None  # type: ignore[assignment]

HINTS = {
    "invalid_arguments": "Fix the named flag and run the command again.",
    "device_not_allowed": ("android:emulator-5554 is never allowed; pass another device with "
                           "--allow-device."),
    "home_invalid": "Pass an existing directory with --home.",
    "source_invalid": ("Run the script from an Autonom checkout that holds "
                       "integrations/chatgpt/autonom-chatgpt-probe."),
    "marketplace_invalid": ("Fix ~/.agents/plugins/marketplace.json by hand (it must be a JSON object "
                            "with a plugins list), then run the command again."),
    "entry_conflict": ("Another marketplace entry is named autonom-chatgpt-probe; rename or remove it "
                       "by hand, then run the command again."),
    "target_conflict": ("~/plugins/autonom-chatgpt-probe was not created by this script; move it "
                        "away by hand, then run install again."),
    "config_invalid": ("Fix or delete the probe config file (it must hold a JSON object), then run "
                       "install again."),
    "cache_conflict": ("The cache directory holds a plugin that is not the probe; remove it by hand "
                       "or run uninstall without --purge-cache."),
    "write_failed": "Check the permissions of the named path and run the command again.",
}


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

def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso_z(moment: _dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def excluded_file(name: str) -> bool:
    return name in EXCLUDED_FILES or name.endswith(EXCLUDED_SUFFIXES)


class UnreadableFile(OSError):
    """A file in a tree that cannot be hashed (unreadable, dangling link, gone)."""

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__("%s: %s" % (path, type(cause).__name__))
        self.path = str(path)
        self.reason = type(cause).__name__


def tree_hashes(root: Path, *, skip_stamp: bool = False) -> dict[str, str]:
    """``{relative posix path: sha256}`` of every file, minus caches (and the stamp).

    Raises ``UnreadableFile`` (an ``OSError``) for a file that cannot be read.
    """
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            if excluded_file(name):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if skip_stamp and rel == STAMP_NAME:
                continue
            try:
                out[rel] = sha256_file(path)
            except OSError as exc:
                raise UnreadableFile(path, exc) from None
    return out


def copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in EXCLUDED_DIRS or excluded_file(name)}


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` through a temp file in the same directory."""
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def git_head(source: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or len(head) not in (40, 64):
        return None
    return head


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
        self.config = Path(os.path.abspath(probe_env.state_dir(env, home) / "config.json"))
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


def resolve_source(env: dict[str, str]) -> tuple[Path, str]:
    """The plugin tree to install and its manifest version."""
    raw = env.get(SOURCE_ENV)
    source = Path(os.path.abspath(raw)) if raw else REPO_SOURCE
    manifest = source / ".codex-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise Refusal("source_invalid", "The probe plugin source has no readable manifest",
                      path=str(source)) from None
    if not isinstance(data, dict) or data.get("name") != PLUGIN_NAME or not isinstance(
            data.get("version"), str):
        raise Refusal("source_invalid", "The probe plugin source manifest is not the probe's",
                      path=str(source))
    return source, data["version"]


# ---------------------------------------------------------------- marketplace

def read_marketplace(path: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    """(raw bytes, parsed object) or (None, None) when the file does not exist."""
    if not os.path.lexists(path):
        return None, None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Refusal("marketplace_invalid", "The marketplace file cannot be read (%s)"
                      % type(exc).__name__, path=str(path)) from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal("marketplace_invalid", "The marketplace file is not valid JSON",
                      path=str(path)) from None
    if not isinstance(data, dict):
        raise Refusal("marketplace_invalid", "The marketplace file is not a JSON object",
                      path=str(path))
    if not isinstance(data.get("plugins"), list):
        raise Refusal("marketplace_invalid", "The marketplace file has no plugins list",
                      path=str(path))
    return raw, data


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
                          "A marketplace entry named autonom-chatgpt-probe points elsewhere",
                          path=str(path), found_path=found if isinstance(found, str) else None)
    return indexes


def plugin_names(data: dict[str, Any] | None) -> list[str]:
    if data is None:
        return []
    return [item["name"] for item in data["plugins"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)]


def marketplace_name(data: dict[str, Any] | None) -> str:
    name = data.get("name") if data else None
    if isinstance(name, str) and name and name not in (".", "..") and "/" not in name \
            and "\\" not in name and "\0" not in name:
        return name
    return DEFAULT_MARKETPLACE_NAME


def file_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return DEFAULT_MARKETPLACE_MODE


def next_backup_path(layout: Layout, moment: _dt.datetime) -> Path:
    base = "marketplace-%s" % moment.strftime("%Y%m%dT%H%M%SZ")
    candidate = layout.backup_dir / (base + ".json")
    number = 1
    while os.path.lexists(candidate):
        candidate = layout.backup_dir / ("%s-%d.json" % (base, number))
        number += 1
    return candidate


def write_backup(layout: Layout, raw: bytes) -> Path:
    """Exact byte copy of the marketplace; never overwrites an older backup."""
    layout.backup_dir.mkdir(parents=True, exist_ok=True)
    moment = utc_now()
    while True:
        target = next_backup_path(layout, moment)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return target


def write_marketplace(path: Path, data: dict[str, Any], mode: int) -> None:
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, dump_json(data), mode)


def list_backups(layout: Layout) -> list[str]:
    try:
        names = sorted(p.name for p in layout.backup_dir.iterdir()
                       if p.is_file() and p.name.startswith("marketplace-"))
    except OSError:
        return []
    return [str(layout.backup_dir / name) for name in names]


# ---------------------------------------------------------------- copy and stamp

def read_stamp(layout: Layout) -> dict[str, Any] | None:
    """The stamp when the copy is a real directory holding the probe's stamp."""
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
        shutil.copytree(layout.source, tmp, symlinks=False, ignore=copy_ignore)
        stamp = dict(stamp_fields)
        stamp["files"] = tree_hashes(tmp)
        atomic_write(tmp / STAMP_NAME, dump_json(stamp), 0o644)
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
        raise Refusal("config_invalid", "The probe config file is not readable JSON",
                      path=str(path)) from None
    if not isinstance(data, dict):
        raise Refusal("config_invalid", "The probe config file is not a JSON object",
                      path=str(path))
    return data


def write_config(path: Path, data: dict[str, Any]) -> None:
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(path.parent, 0o700)
    atomic_write(path, dump_json(data), 0o600)


def config_path_is_ours(raw: Any) -> bool:
    """Uninstall only deletes a path that looks like the probe config."""
    if not isinstance(raw, str) or not os.path.isabs(raw):
        return False
    path = Path(raw)
    return path.name == "config.json" and path.parent.name == "chatgpt-probe"


def bad_flag(flag: str, problem: str) -> Refusal:
    return Refusal("invalid_arguments", "%s: %s" % (flag, problem), exit_code=2,
                   field=flag, problem=problem)


def config_updates(args: argparse.Namespace) -> dict[str, Any]:
    """Validated config keys named by flags (contract 2.3 rules)."""
    updates: dict[str, Any] = {}
    if args.media_port is not None:
        if not 1024 <= args.media_port <= 65535:
            raise bad_flag("--media-port", "must be an integer 1024-65535")
        updates["media_port"] = args.media_port
    if args.remote_route:
        routes: list[dict[str, str]] = []
        for raw in args.remote_route:
            label, sep, origin = raw.partition("=")
            if not sep:
                raise bad_flag("--remote-route", "must be LABEL=ORIGIN")
            if not 1 <= len(label) <= 40:
                raise bad_flag("--remote-route", "the label must be 1-40 characters")
            origin = origin.lower()
            if not probe_env.valid_route_origin(origin):
                raise bad_flag("--remote-route",
                               "the origin must be http://127.0.0.1:N or https://host[:port]")
            if any(route["label"] == label for route in routes):
                raise bad_flag("--remote-route", "the label %r is given twice" % label)
            routes.append({"label": label, "origin": origin})
        if len(routes) > MAX_REMOTE_ROUTES:
            raise bad_flag("--remote-route", "at most %d routes" % MAX_REMOTE_ROUTES)
        updates["remote_routes"] = routes
    if args.allow_origin:
        origins: list[str] = []
        for raw in args.allow_origin:
            origin = raw.lower()
            if not probe_env.valid_allow_origin(origin):
                raise bad_flag("--allow-origin",
                               "must be an exact origin scheme://host[:port] or https://*.suffix")
            if origin not in origins:
                origins.append(origin)
        updates["allow_origins"] = origins
    if args.allow_device:
        devices: list[str] = []
        for spec in args.allow_device:
            if spec == probe_env.REFUSED_DEVICE:
                raise Refusal("device_not_allowed", "%s is never allowed" % spec,
                              field="--allow-device")
            if not probe_env.valid_device_spec(spec):
                raise bad_flag("--allow-device", "must be android:<serial> or ios:<udid>")
            if spec not in devices:
                devices.append(spec)
        updates["allowed_devices"] = devices
    return updates


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
    except UnreadableFile as exc:
        raise Refusal("source_invalid", "A file in the probe plugin source cannot be read (%s)" % exc.reason,
                      path=exc.path) from None
    copy_needed = True
    if previous is not None:
        try:
            copy_files = tree_hashes(layout.copy, skip_stamp=True)
        except UnreadableFile as exc:
            raise Refusal("write_failed", "A file in the installed copy cannot be read (%s)" % exc.reason,
                          path=exc.path) from None
        copy_needed = not (previous.get("files") == copy_files == source_files)

    prev = previous or {}
    prev_backup = prev.get("first_backup") if isinstance(prev.get("first_backup"), str) else None
    backup_needed = market_changed and raw_market is not None
    planned_backup = next_backup_path(layout, utc_now()) if backup_needed else None
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
        actions.append({"op": "write_config", "path": str(layout.config)})
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
            made = write_backup(layout, raw_market or b"")
            for action in actions:
                if action["op"] == "backup":
                    action["path"] = str(made)
            stamp_fields["first_backup"] = prev_backup or str(made)
        if copy_needed:
            current = str(layout.copy)
            stamp_fields["installed_at"] = iso_z(utc_now())
            stamp_fields["source_commit"] = git_head(source)
            copy_tree(layout, stamp_fields)
        elif stamp_needed:
            current = str(layout.stamp)
            refreshed = dict(previous or {})
            for key in stamp_keys + ("first_backup",):
                refreshed[key] = stamp_fields[key]
            atomic_write(layout.stamp, dump_json(refreshed), 0o644)
        if market_changed:
            current = str(layout.marketplace)
            write_marketplace(layout.marketplace, new_market,
                              file_mode(layout.marketplace) if raw_market is not None
                              else DEFAULT_MARKETPLACE_MODE)
        if config_changed:
            current = str(layout.config)
            write_config(layout.config, new_config)
    except OSError as exc:
        raise Refusal("write_failed", "Writing failed (%s)" % type(exc).__name__,
                      path=current, actions=actions) from None
    return result


def cache_check(cache_dir: Path) -> bool:
    """True when the cache dir exists and is the probe's; refuses a foreign one."""
    if not os.path.lexists(cache_dir):
        return False
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        raise Refusal("cache_conflict", "The probe cache path is not a plain directory",
                      path=str(cache_dir))
    manifests: list[Path] = []
    has_files = False
    for dirpath, dirnames, filenames in os.walk(cache_dir):
        if filenames:
            has_files = True
        if Path(dirpath).name == ".codex-plugin" and "plugin.json" in filenames:
            manifests.append(Path(dirpath) / "plugin.json")
    if has_files and not manifests:
        raise Refusal("cache_conflict", "The probe cache directory holds files but no plugin manifest",
                      path=str(cache_dir))
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            data = None
        if not isinstance(data, dict) or data.get("name") != PLUGIN_NAME:
            raise Refusal("cache_conflict", "A plugin manifest in the probe cache is not the probe's",
                          path=str(manifest))
    return True


def cmd_uninstall(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    home = resolve_home(args.home)
    layout = Layout(home, env, REPO_SOURCE)

    raw_market, market = read_marketplace(layout.marketplace)
    indexes = check_entries(market, layout.marketplace)
    stamp = read_stamp(layout)
    cache_dir = layout.cache_dir(marketplace_name(market))
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
        actions.append({"op": "backup", "path": str(next_backup_path(layout, utc_now())),
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
            kept.append({"path": str(raw_path), "reason": "stamp config_path is not a probe config"})
    elif os.path.lexists(layout.config):
        kept.append({"path": str(layout.config), "reason": "config not created by install"})

    if purge:
        actions.append({"op": "remove", "path": str(cache_dir)})

    result: dict[str, Any] = {
        "ok": True, "command": "uninstall", "dry_run": bool(args.dry_run),
        "changed": bool(actions), "actions": actions, "kept": kept,
        "manual_steps": ("Uninstall Autonom probe in ChatGPT desktop (Plugins) and remove its "
                         "entry from ~/.codex/config.toml if one remains; this script never "
                         "edits that file."),
    }
    if args.dry_run:
        return result

    current = ""
    try:
        if new_market is not None:
            current = str(layout.backup_dir)
            made = write_backup(layout, raw_market or b"")
            actions[0]["path"] = str(made)
            current = str(layout.marketplace)
            if delete_market:
                os.unlink(layout.marketplace)
            else:
                write_marketplace(layout.marketplace, new_market, file_mode(layout.marketplace))
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
    raw_source = env.get(SOURCE_ENV)
    source = Path(os.path.abspath(raw_source)) if raw_source else REPO_SOURCE
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
        market_info["plugins"] = plugin_names(market)
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
            copy_files: dict[str, str] | None = tree_hashes(layout.copy, skip_stamp=True)
        except UnreadableFile as exc:
            copy_files = None
            copy_info["reason"] = "unreadable file in the copy: %s (%s)" % (exc.path, exc.reason)
        copy_info["stamp_ok"] = (copy_files is not None and stamp.get("schema") == STAMP_SCHEMA
                                 and stamp.get("files") == copy_files)
        try:
            source_files = tree_hashes(source) if source.is_dir() else None
        except UnreadableFile as exc:
            source_files = None
            copy_info["source_reason"] = "unreadable file in the source: %s (%s)" % (exc.path, exc.reason)
        copy_info["files_match_source"] = (copy_files is not None and source_files is not None
                                           and copy_files == source_files)

    cache_dir = layout.cache_dir(marketplace_name(market))
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
        "config": config_info, "backups": list_backups(layout), "source": str(source),
    }


# ---------------------------------------------------------------- output and main

def human(result: dict[str, Any]) -> str:
    command = result.get("command")
    lines: list[str] = []
    if command == "status":
        lines.append("Autonom probe installed: %s" % ("yes" if result["installed"] else "no"))
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


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="install_probe.py",
        description="Install, uninstall or inspect the Autonom ChatGPT probe dev plugin.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, dry_run: bool = True) -> None:
        p.add_argument("--home", help="home directory to work in (default: your home)")
        p.add_argument("--json", action="store_true", help="print one JSON object")
        if dry_run:
            p.add_argument("--dry-run", action="store_true", help="check and plan, write nothing")

    install = sub.add_parser("install", help="copy the probe and add its marketplace entry")
    common(install)
    install.add_argument("--media-port", type=int, help="probe media port (1024-65535)")
    install.add_argument("--remote-route", action="append", default=[], metavar="LABEL=ORIGIN",
                         help="extra media route, e.g. forwarded=http://127.0.0.1:47812")
    install.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                         help="extra allowed page origin (exact, or https://*.suffix)")
    install.add_argument("--allow-device", action="append", default=[], metavar="SPEC",
                         help="device the probe may relay: android:<serial> or ios:<udid>")

    uninstall = sub.add_parser("uninstall", help="remove what install added")
    common(uninstall)
    uninstall.add_argument("--purge-cache", action="store_true",
                           help="also remove the probe's own ChatGPT plugin cache directory")

    status = sub.add_parser("status", help="report what is installed")
    common(status, dry_run=False)
    return parser


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


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    JsonArgumentParser.json_errors = "--json" in raw_argv
    args = build_parser().parse_args(raw_argv)
    env = dict(os.environ)
    handlers = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}
    try:
        if probe_env is None:
            raise Refusal("source_invalid", "The probe plugin source next to this script is missing",
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
