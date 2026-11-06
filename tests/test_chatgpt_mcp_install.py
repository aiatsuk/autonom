"""Autonom MCP adapter install / uninstall / status script (contract section 7, task install).

Every test runs ``integrations/chatgpt/scripts/install_workspace.py`` as a real
subprocess with ``--home <temp dir>``, ``HOME`` set to that temp dir and a
sandboxed ``AUTONOM_HOME`` and ``XDG_STATE_HOME``, so nothing is ever written to
the real ``~/.agents``, ``~/plugins``, ``~/.codex``, ``~/.autonom`` or
``~/.local/state/autonom``. No test touches a device or a host service.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

try:
    from env_isolation import EnvSandboxMixin  # discover -s tests / run_parallel
except ImportError:  # python3 -m unittest tests.test_chatgpt_mcp_install
    from tests.env_isolation import EnvSandboxMixin

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "integrations" / "chatgpt" / "scripts"
SCRIPT = SCRIPTS / "install_workspace.py"
PROBE_SCRIPT = SCRIPTS / "install_probe.py"
SOURCE = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt"
README = ROOT / "integrations" / "chatgpt" / "README.md"
NAME = "autonom-chatgpt"
PROBE = "autonom-chatgpt-probe"
STAMP = ".autonom-chatgpt-install.json"
ENTRY = {
    "name": NAME,
    "source": {"source": "local", "path": "./plugins/autonom-chatgpt"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
}
PROBE_ENTRY = {
    "name": PROBE,
    "source": {"source": "local", "path": "./plugins/autonom-chatgpt-probe"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
}
HARNESS_ENTRY = {
    "name": "delivery-harness",
    "source": {"source": "local", "path": "./plugins/delivery-harness"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity",
}
# The owner's personal marketplace on the development Mac, byte for byte in shape,
# with the probe entry installed next to it.
PERSONAL = json.dumps({"name": "personal", "interface": {"displayName": "Personal"},
                       "plugins": [HARNESS_ENTRY, PROBE_ENTRY]}, indent=2) + "\n"
NEXT_STEPS = ("Restart ChatGPT desktop, open Plugins, choose the Personal marketplace, "
              "install Autonom (dev), enable it in Work mode.")
CODEX_CONFIG = """model = "example-model"
secret_value = "do-not-print-me"

[plugins."sentry@x"]
enabled = true

[plugins."sentry@x".env]
token = "do-not-print-me-either"

[plugins."autonom-chatgpt-probe@personal"]
enabled = false

  [ plugins . "my.tool.v2@personal" ]  # dotted plugin name
enabled = true

[plugins.'literal@market']
enabled = true

[[plugins."array@x"]]
# [plugins."commented@x"]
[mcp_servers."not-a-plugin@x"]
[plugins."no-marketplace"]
note = '''
'''
"""


def snapshot(root: Path) -> dict[str, Any]:
    """Every path under ``root`` with its type, mode and bytes."""
    out: dict[str, Any] = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = os.lstat(path)
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                out[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                out[rel] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
    return out


def tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name == ".DS_Store" or name.endswith(".pyc"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if rel == STAMP:
                continue
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstallWorkspaceTestBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.state = self.sandbox_home()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.xdg = self.tmp / "xdg-state"
        self.xdg.mkdir()
        self.market = self.home / ".agents" / "plugins" / "marketplace.json"
        self.copy = self.home / "plugins" / NAME
        self.backups = self.home / "plugins" / "backups" / NAME
        self.config = self.state / "chatgpt-mcp" / "config.json"
        self.extra_env: dict[str, str] = {}

    # -- helpers

    def child_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        child = dict(os.environ)
        child["HOME"] = str(self.home)
        child["AUTONOM_HOME"] = str(self.state)
        child["XDG_STATE_HOME"] = str(self.xdg)
        child["PYTHONDONTWRITEBYTECODE"] = "1"
        child.pop("AUTONOM_PROBE_INSTALL_SOURCE", None)
        child.pop("AUTONOM_MCP_INSTALL_SOURCE", None)
        child.update(self.extra_env)
        for key, value in (env or {}).items():
            if value is None:
                child.pop(key, None)
            else:
                child[key] = value
        return child

    def run_script(self, *args: str, home: Path | None = None, json_out: bool = True,
                   env: dict[str, Any] | None = None, script: Path = SCRIPT) -> tuple[int, Any, str]:
        argv = [sys.executable, str(script), *args, "--home", str(home or self.home)]
        if json_out:
            argv.append("--json")
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.child_env(env), timeout=60, check=False)
        out = proc.stdout.decode("utf-8")
        err = proc.stderr.decode("utf-8")
        self.assertNotIn("Traceback", err)
        if json_out and out.strip():
            lines = [line for line in out.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1, "expected one JSON line, got %r" % out)
            return proc.returncode, json.loads(lines[0]), err
        return proc.returncode, out, err

    def write_personal(self, text: str = PERSONAL) -> bytes:
        self.market.parent.mkdir(parents=True, exist_ok=True)
        self.market.write_text(text, encoding="utf-8")
        return self.market.read_bytes()

    def install(self, *flags: str) -> dict[str, Any]:
        code, result, err = self.run_script("install", *flags)
        self.assertEqual(code, 0, (result, err))
        self.assertTrue(result["ok"])
        return result

    def uninstall(self, *flags: str) -> dict[str, Any]:
        code, result, err = self.run_script("uninstall", *flags)
        self.assertEqual(code, 0, (result, err))
        self.assertTrue(result["ok"])
        return result

    def status(self) -> dict[str, Any]:
        code, result, err = self.run_script("status")
        self.assertEqual(code, 0, (result, err))
        self.assertTrue(result["ok"])
        return result

    def assert_refusal(self, code: int, result: Any, error_code: str, exit_code: int = 1) -> None:
        self.assertEqual(code, exit_code, result)
        self.assertIsInstance(result, dict)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error_code"], error_code)
        self.assertIsInstance(result["error"], str)
        self.assertTrue(result["error"])
        self.assertIsInstance(result["hint"], str)
        self.assertTrue(result["hint"])

    def stamp(self) -> dict[str, Any]:
        return json.loads((self.copy / STAMP).read_text(encoding="utf-8"))

    def backup_files(self) -> list[Path]:
        if not self.backups.is_dir():
            return []
        return sorted(self.backups.iterdir())

    def assert_no_codex(self) -> None:
        self.assertFalse((self.home / ".codex").exists(), "nothing may be written under ~/.codex")

    def make_cache(self, version: str, name: str = NAME, marketplace: str = "personal",
                   plugin_dir: str = NAME) -> Path:
        base = self.home / ".codex" / "plugins" / "cache" / marketplace / plugin_dir
        manifest = base / version / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8")
        (base / version / "server").mkdir()
        (base / version / "server" / "mcp_server.py").write_text("# cached\n", encoding="utf-8")
        return base


class InstallTests(InstallWorkspaceTestBase):
    def test_install_into_personal_marketplace_keeps_other_entries(self) -> None:
        before = self.write_personal()
        flows = self.tmp / "flows"
        flows.mkdir()
        cli = ROOT / "scripts" / "autonom.py"
        result = self.install("--allow-target", "android:emulator-5584",
                              "--allow-target", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5",
                              "--flow-root", str(flows), "--canvas-workspace", "chatgpt-dev",
                              "--control-idle", "30", "--autonom-cli", str(cli))
        self.assertTrue(result["changed"])
        self.assertEqual(result["next_steps"], NEXT_STEPS)
        self.assertEqual([a["op"] for a in result["actions"]],
                         ["copy", "backup", "write_marketplace", "write_config"])

        data = json.loads(self.market.read_text(encoding="utf-8"))
        original = json.loads(before)
        self.assertEqual(list(data), list(original))  # top-level key order
        self.assertEqual(data["interface"], original["interface"])
        self.assertEqual(data["plugins"][:2], original["plugins"])
        self.assertEqual([list(p) for p in data["plugins"][:2]], [list(p) for p in original["plugins"]])
        self.assertEqual(data["plugins"][2], ENTRY)
        self.assertEqual(len(data["plugins"]), 3)
        self.assertTrue(self.market.read_text(encoding="utf-8").endswith("}\n"))
        self.assertEqual(stat.S_IMODE(self.market.stat().st_mode), 0o644)

        backups = self.backup_files()
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"^marketplace-\d{8}T\d{6}Z(-\d+)?\.json$")
        self.assertEqual(backups[0].read_bytes(), before)

        stamp = self.stamp()
        self.assertEqual(stamp["schema"], 1)
        self.assertEqual(stamp["plugin"], NAME)
        self.assertEqual(stamp["version"], "0.1.0")
        self.assertEqual(stamp["source"], str(SOURCE))
        self.assertTrue(stamp["source_commit"] is None or len(stamp["source_commit"]) == 40)
        self.assertRegex(stamp["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIs(stamp["created_marketplace"], False)
        self.assertIs(stamp["wrote_config"], True)
        self.assertEqual(stamp["config_path"], str(self.config))
        self.assertEqual(stamp["first_backup"], str(backups[0]))
        self.assertEqual(stamp["files"], tree_hashes(SOURCE))

        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config, {
            "flow_roots": [str(flows)],
            "allowed_targets": ["android:emulator-5584", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"],
            "canvas_workspace": "chatgpt-dev", "control_idle_s": 30, "autonom_cli": str(cli)})
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        self.assertFalse(self.copy.is_symlink())
        self.assertTrue(os.access(self.copy / "bin" / "launch.sh", os.X_OK))
        self.assertTrue((self.copy / ".mcp.json").is_file())
        self.assert_no_codex()
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".agents", "plugins"])
        self.assertEqual(sorted(p.name for p in (self.home / "plugins").iterdir()),
                         sorted(["backups", NAME]))
        self.assertEqual(list(self.xdg.iterdir()), [], "AUTONOM_HOME wins over XDG_STATE_HOME")

    def test_coexistence_and_restore_scenario(self) -> None:
        """p3-016-S01: install, install again, uninstall next to the harness and probe entries."""
        before = self.write_personal()
        self.install("--allow-target", "android:emulator-5584")
        result = self.install("--allow-target", "android:emulator-5584")
        self.assertIs(result["changed"], False)
        data = json.loads(self.market.read_text(encoding="utf-8"))
        self.assertEqual([p["name"] for p in data["plugins"]], ["delivery-harness", PROBE, NAME])
        self.assertEqual(data["plugins"][0], HARNESS_ENTRY)
        self.assertEqual(data["plugins"][1], PROBE_ENTRY)
        self.assertEqual(len(self.backup_files()), 1)

        result = self.uninstall()
        self.assertTrue(result["changed"])
        self.assertIn("config.toml", result["manual_steps"])
        self.assertEqual(json.loads(self.market.read_bytes()), json.loads(before))
        self.assertFalse(os.path.lexists(self.copy))
        self.assertFalse(self.config.exists())
        self.assert_no_codex()
        self.assertEqual(sorted(p.name for p in (self.home / "plugins").iterdir()), ["backups"])

    def test_coexists_with_the_real_probe_installer(self) -> None:
        self.write_personal(json.dumps({"name": "personal", "plugins": [HARNESS_ENTRY]}, indent=2) + "\n")
        code, probe, err = self.run_script("install", script=PROBE_SCRIPT)
        self.assertEqual(code, 0, (probe, err))
        self.install()
        names = [p["name"] for p in json.loads(self.market.read_text(encoding="utf-8"))["plugins"]]
        self.assertEqual(names, ["delivery-harness", PROBE, NAME])
        probe_copy = self.home / "plugins" / PROBE
        probe_copy_before = snapshot(probe_copy)
        probe_config = self.state / "chatgpt-probe" / "config.json"
        self.assertTrue(probe_config.exists())

        self.uninstall("--purge-cache")
        names = [p["name"] for p in json.loads(self.market.read_text(encoding="utf-8"))["plugins"]]
        self.assertEqual(names, ["delivery-harness", PROBE])
        self.assertEqual(snapshot(probe_copy), probe_copy_before)
        self.assertTrue(probe_config.exists())
        # The probe's own status still sees its install intact.
        code, status, err = self.run_script("status", script=PROBE_SCRIPT)
        self.assertEqual(code, 0, (status, err))
        self.assertIs(status["installed"], True)

    def test_install_into_empty_home_creates_marketplace(self) -> None:
        result = self.install()
        self.assertNotIn("backup", [a["op"] for a in result["actions"]])
        data = json.loads(self.market.read_text(encoding="utf-8"))
        self.assertEqual(data, {"name": "personal", "interface": {"displayName": "Personal"},
                                "plugins": [ENTRY]})
        self.assertEqual(stat.S_IMODE(self.market.stat().st_mode), 0o644)
        stamp = self.stamp()
        self.assertIs(stamp["created_marketplace"], True)
        self.assertIsNone(stamp["first_backup"])
        self.assertIs(stamp["wrote_config"], True)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {})
        self.assertEqual(self.backup_files(), [])
        self.assert_no_codex()

    def test_reinstall_is_idempotent_and_keeps_first_install_facts(self) -> None:
        self.write_personal()
        self.install("--control-idle", "60")
        first = self.stamp()
        backups = self.backup_files()
        market_bytes = self.market.read_bytes()
        config_bytes = self.config.read_bytes()

        result = self.install("--control-idle", "60")
        self.assertIs(result["changed"], False)
        self.assertEqual(result["actions"], [])
        self.assertEqual(self.backup_files(), backups)
        self.assertEqual(self.market.read_bytes(), market_bytes)
        self.assertEqual(self.config.read_bytes(), config_bytes)
        self.assertEqual(self.stamp(), first)

        # No flags at all: nothing changes either (flags only replace their own keys).
        result = self.install()
        self.assertEqual(result["actions"], [])

        # A flag change rewrites the config only; the stamp keeps wrote_config.
        result = self.install("--control-idle", "90")
        self.assertEqual([a["op"] for a in result["actions"]], ["write_config"])
        self.assertEqual(result["actions"][0]["keys"], ["control_idle_s"])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"control_idle_s": 90})
        self.assertIs(self.stamp()["wrote_config"], True)

        # A changed entry is replaced in place, after a new backup; the first backup stays first.
        data = json.loads(market_bytes)
        data["plugins"][2]["category"] = "Other"
        data["plugins"].append({"name": "later", "source": {"source": "local", "path": "./plugins/later"}})
        self.market.write_text(json.dumps(data), encoding="utf-8")
        edited = self.market.read_bytes()
        result = self.install()
        self.assertEqual([a["op"] for a in result["actions"]], ["backup", "write_marketplace"])
        new = json.loads(self.market.read_text(encoding="utf-8"))
        self.assertEqual([p["name"] for p in new["plugins"]], ["delivery-harness", PROBE, NAME, "later"])
        self.assertEqual(new["plugins"][2], ENTRY)
        backups_now = self.backup_files()
        self.assertEqual(len(backups_now), len(backups) + 1)
        self.assertIn(edited, [p.read_bytes() for p in backups_now])
        self.assertEqual(self.stamp()["first_backup"], first["first_backup"])

    def test_refreshed_copy_keeps_created_marketplace_and_wrote_config(self) -> None:
        self.install()
        (self.copy / "bin" / "launch.sh").write_text("changed\n", encoding="utf-8")
        result = self.install("--canvas-workspace", "other")
        self.assertIn("copy", [a["op"] for a in result["actions"]])
        stamp = self.stamp()
        self.assertIs(stamp["created_marketplace"], True)
        self.assertIs(stamp["wrote_config"], True)
        self.assertEqual((self.copy / "bin" / "launch.sh").read_bytes(),
                         (SOURCE / "bin" / "launch.sh").read_bytes())
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")),
                         {"canvas_workspace": "other"})
        self.assertEqual(json.loads(self.market.read_text(encoding="utf-8"))["plugins"], [ENTRY])

    def test_duplicate_own_entries_collapse_to_one(self) -> None:
        self.market.parent.mkdir(parents=True)
        self.market.write_text(json.dumps({"name": "personal", "plugins": [
            dict(ENTRY, category="A"), PROBE_ENTRY, dict(ENTRY, category="B")]}), encoding="utf-8")
        self.install()
        plugins = json.loads(self.market.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual(plugins, [ENTRY, PROBE_ENTRY])

    def test_dry_run_writes_nothing(self) -> None:
        """p3-016-S02: planned actions printed; marketplace sha256 and ~/plugins listing unchanged."""
        self.write_personal()
        market_sha = sha256(self.market)
        state_before, home_before = snapshot(self.state), snapshot(self.home)
        code, result, _ = self.run_script("install", "--dry-run", "--allow-target", "android:emulator-5584",
                                          "--control-idle", "30")
        self.assertEqual(code, 0, result)
        self.assertIs(result["dry_run"], True)
        self.assertEqual([a["op"] for a in result["actions"]],
                         ["copy", "backup", "write_marketplace", "write_config"])
        for action in result["actions"]:
            self.assertTrue(os.path.isabs(action["path"]))
        self.assertEqual(sha256(self.market), market_sha)
        self.assertFalse((self.home / "plugins").exists())
        self.assertEqual(snapshot(self.home), home_before)
        self.assertEqual(snapshot(self.state), state_before)

        # Also against an installed tree, for uninstall with --purge-cache.
        self.install()
        self.make_cache("local")
        plugins_listing = sorted(os.listdir(self.home / "plugins"))
        home_before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, _ = self.run_script("uninstall", "--dry-run", "--purge-cache")
        self.assertEqual(code, 0, result)
        self.assertEqual([a["op"] for a in result["actions"]],
                         ["backup", "write_marketplace", "remove", "remove", "remove"])
        self.assertEqual(sorted(os.listdir(self.home / "plugins")), plugins_listing)
        self.assertEqual(snapshot(self.home), home_before)
        self.assertEqual(snapshot(self.state), state_before)

    def test_dry_run_without_state_env_plans_the_default_state_path(self) -> None:
        """As ChatGPT starts the server: no AUTONOM_HOME or XDG_STATE_HOME (CASE-008 shape)."""
        self.write_personal()
        home_before = snapshot(self.home)
        code, result, _ = self.run_script(
            "install", "--dry-run", "--allow-target", "android:emulator-5584",
            "--allow-target", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5",
            env={"AUTONOM_HOME": None, "XDG_STATE_HOME": None})
        self.assertEqual(code, 0, result)
        expected = self.home / ".local" / "state" / "autonom" / "chatgpt-mcp" / "config.json"
        self.assertEqual(result["config"], str(expected))
        paths = {a["op"]: a["path"] for a in result["actions"]}
        self.assertEqual(sorted(paths), ["backup", "copy", "write_config", "write_marketplace"])
        self.assertEqual(paths["copy"], str(self.copy))
        self.assertTrue(paths["backup"].startswith(str(self.backups) + os.sep))
        self.assertEqual(paths["write_marketplace"], str(self.market))
        self.assertEqual(paths["write_config"], str(expected))
        self.assertEqual(snapshot(self.home), home_before)

    def test_copy_excludes_caches_and_matches_source(self) -> None:
        source = self.tmp / "src" / NAME
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (source / "server" / "__pycache__").mkdir()
        (source / "server" / "__pycache__" / "mcp_env.cpython-314.pyc").write_bytes(b"\0")
        (source / "server" / "stray.pyc").write_bytes(b"\0")
        (source / ".DS_Store").write_bytes(b"\0")
        (source / "ui" / ".DS_Store").write_bytes(b"\0")
        self.extra_env["AUTONOM_MCP_INSTALL_SOURCE"] = str(source)
        self.install()
        copied = {p.relative_to(self.copy).as_posix() for p in self.copy.rglob("*")}
        self.assertFalse([p for p in copied if "__pycache__" in p or p.endswith((".pyc", ".DS_Store"))],
                         copied)
        stamp = self.stamp()
        self.assertEqual(stamp["source"], str(source))
        self.assertIsNone(stamp["source_commit"])
        self.assertEqual(stamp["files"], tree_hashes(source))
        self.assertEqual(stamp["files"], tree_hashes(SOURCE))
        self.assertNotIn(STAMP, stamp["files"])

    def test_config_merges_with_existing_keys(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"max_streams": 2, "control_idle_s": 120,
                                           "allowed_targets": ["android:old-1"],
                                           "future_key": {"kept": True}}), encoding="utf-8")
        result = self.install("--control-idle", "45", "--allow-target", "android:emulator-5584",
                              "--allow-target", "android:emulator-5584")
        self.assertIn("write_config", [a["op"] for a in result["actions"]])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {
            "max_streams": 2, "control_idle_s": 45, "allowed_targets": ["android:emulator-5584"],
            "future_key": {"kept": True}})
        self.assertIs(self.stamp()["wrote_config"], False)
        # Uninstall keeps a config that install did not create.
        result = self.uninstall()
        self.assertTrue(self.config.exists())
        self.assertIn(str(self.config), [k["path"] for k in result["kept"]])

    def test_flag_values_are_normalised_and_deduplicated(self) -> None:
        one, two = self.tmp / "flows-1", self.tmp / "flows-2"
        one.mkdir()
        two.mkdir()
        self.install("--flow-root", str(one) + "/", "--flow-root", str(two), "--flow-root", str(one),
                     "--autonom-cli", str(ROOT / "scripts" / ".." / "scripts" / "autonom.py"))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["flow_roots"], [str(one), str(two)])
        self.assertEqual(config["autonom_cli"], str(ROOT / "scripts" / "autonom.py"))

    def test_human_output(self) -> None:
        code, out, _ = self.run_script("install", json_out=False)
        self.assertEqual(code, 0)
        self.assertIn("Done: copy", out)
        self.assertIn(NEXT_STEPS, out)
        code, out, _ = self.run_script("status", json_out=False)
        self.assertEqual(code, 0)
        self.assertIn("Autonom (dev) installed: yes", out)
        self.assertIn("ChatGPT config plugins: none", out)
        code, out, err = self.run_script("install", "--control-idle", "5", json_out=False)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("invalid_arguments", err)
        self.assertIn("hint:", err)
        code, out, _ = self.run_script("uninstall", json_out=False)
        self.assertEqual(code, 0)
        self.assertIn("Manual: ", out)

    def test_no_bytecode_is_written_next_to_the_helpers_or_into_the_plugin(self) -> None:
        caches = [SCRIPTS / "__pycache__", SOURCE / "server" / "__pycache__"]
        existed = {str(path): path.exists() for path in caches}
        before = {str(path): sorted(os.listdir(path)) for path in caches if path.exists()}
        argv = [sys.executable, str(SCRIPT), "status", "--json", "--home", str(self.home)]
        env = self.child_env()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for path in caches:
            if not existed[str(path)]:
                self.assertFalse(path.exists(), path)
            else:
                new = set(os.listdir(path)) - set(before[str(path)])
                self.assertFalse([n for n in new if n.startswith(("install_", "mcp_env", "mcp_common"))],
                                 new)


class StatusTests(InstallWorkspaceTestBase):
    def write_codex(self, text: str = CODEX_CONFIG) -> Path:
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_status_fields(self) -> None:
        self.write_personal()
        status = self.status()
        self.assertIs(status["installed"], False)
        self.assertIsNone(status["entry"])
        self.assertEqual(status["copy"]["path"], str(self.copy))
        self.assertIs(status["copy"]["stamp_ok"], False)
        self.assertEqual(status["marketplace"]["path"], str(self.market))
        self.assertEqual(status["marketplace"]["plugins"], ["delivery-harness", PROBE])
        self.assertIs(status["cache"]["present"], False)
        self.assertEqual(status["cache"]["path"],
                         str(self.home / ".codex" / "plugins" / "cache" / "personal" / NAME))
        self.assertIs(status["config"]["exists"], False)
        self.assertEqual(status["backups"], [])
        self.assertEqual(status["chatgpt"], {"config_plugins": [], "cache_plugins": [], "problems": []})
        self.assert_no_codex()

        self.install("--control-idle", "33")
        self.make_cache("0.1.0")
        status = self.status()
        self.assertIs(status["installed"], True)
        self.assertEqual(status["entry"], ENTRY)
        self.assertIs(status["copy"]["stamp_ok"], True)
        self.assertIs(status["copy"]["files_match_source"], True)
        self.assertEqual(status["copy"]["version"], "0.1.0")
        self.assertEqual(status["marketplace"]["plugins"], ["delivery-harness", PROBE, NAME])
        self.assertEqual(status["cache"], {"present": True, "versions": ["0.1.0"],
                                           "path": status["cache"]["path"]})
        self.assertEqual(status["config"]["values"], {"control_idle_s": 33})
        self.assertEqual(status["config"]["path"], str(self.config))
        self.assertEqual(len(status["backups"]), 1)
        self.assertEqual(status["chatgpt"]["cache_plugins"], [NAME + "@personal"])

        # A tampered copy shows up as stale.
        (self.copy / "bin" / "launch.sh").write_text("x\n", encoding="utf-8")
        status = self.status()
        self.assertIs(status["copy"]["stamp_ok"], False)
        self.assertIs(status["copy"]["files_match_source"], False)
        self.assertIs(status["installed"], False)

        # Invalid marketplace: status still answers, flagged invalid.
        self.market.write_text("{nope", encoding="utf-8")
        status = self.status()
        self.assertIs(status["marketplace"]["valid"], False)
        self.assertEqual(status["marketplace"]["error_code"], "marketplace_invalid")

    def test_chatgpt_inventory_scenario(self) -> None:
        """p3-016-S03, plus a dotted name, a literal key and lines that are not plugin headers."""
        config = self.write_codex()
        digest = sha256(config)
        self.make_cache("0.1.0", name=PROBE, plugin_dir=PROBE)
        (self.home / ".codex" / "plugins" / "cache" / "x" / "sentry" / "1.0").mkdir(parents=True)
        (self.home / ".codex" / "plugins" / "cache" / "personal" / "stray-file").write_text(
            "x", encoding="utf-8")
        codex_before = snapshot(self.home / ".codex")

        status = self.status()
        self.assertEqual(status["chatgpt"]["config_plugins"], [
            "autonom-chatgpt-probe@personal", "literal@market", "my.tool.v2@personal", "sentry@x"])
        self.assertEqual(status["chatgpt"]["cache_plugins"], ["autonom-chatgpt-probe@personal",
                                                             "sentry@x"])
        self.assertEqual(status["chatgpt"]["problems"], [])
        self.assertEqual(sha256(config), digest)
        self.assertEqual(snapshot(self.home / ".codex"), codex_before)
        dumped = json.dumps(status)
        self.assertNotIn("do-not-print-me", dumped)
        self.assertNotIn("example-model", dumped)

        # The exact spec fixture: two tables and one cache folder.
        self.write_codex('[plugins."sentry@x"]\nenabled = true\n\n'
                         '[plugins."autonom-chatgpt-probe@personal"]\nenabled = true\n')
        shutil.rmtree(self.home / ".codex" / "plugins")
        self.make_cache("0.1.0", name=PROBE, plugin_dir=PROBE)
        digest = sha256(config)
        status = self.status()
        self.assertEqual(status["chatgpt"]["config_plugins"], ["autonom-chatgpt-probe@personal", "sentry@x"])
        self.assertEqual(status["chatgpt"]["cache_plugins"], ["autonom-chatgpt-probe@personal"])
        self.assertEqual(sha256(config), digest)

    def test_inventory_missing_or_unreadable_sources_give_empty_lists(self) -> None:
        # Missing file and folder.
        status = self.status()
        self.assertEqual(status["chatgpt"]["config_plugins"], [])
        self.assertEqual(status["chatgpt"]["cache_plugins"], [])
        self.assert_no_codex()
        # A directory where config.toml should be, and a cache root that is a file.
        (self.home / ".codex" / "config.toml").mkdir(parents=True)
        (self.home / ".codex" / "plugins").mkdir()
        (self.home / ".codex" / "plugins" / "cache").write_text("not a dir", encoding="utf-8")
        status = self.status()
        self.assertEqual(status["chatgpt"]["config_plugins"], [])
        self.assertEqual(status["chatgpt"]["cache_plugins"], [])
        self.assertEqual(len(status["chatgpt"]["problems"]), 1)
        self.assertIn("config.toml", status["chatgpt"]["problems"][0])
        # Bytes that are not UTF-8 do not break the scan.
        shutil.rmtree(self.home / ".codex")
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'\xff\xfe\n[plugins."ok@x"]\n')
        self.assertEqual(self.status()["chatgpt"]["config_plugins"], ["ok@x"])
        # A market folder that cannot be listed is reported, the others still counted.
        cache = self.home / ".codex" / "plugins" / "cache"
        (cache / "good" / "tool").mkdir(parents=True)
        locked = cache / "locked"
        (locked / "hidden").mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o755))
        if not os.access(str(locked), os.R_OK):  # root can read anything
            inventory = self.status()["chatgpt"]
            self.assertEqual(inventory["cache_plugins"], ["tool@good"])
            self.assertEqual(len(inventory["problems"]), 1)
            self.assertIn("locked", inventory["problems"][0])
        locked.chmod(0o755)

    def test_inventory_follows_install_and_uninstall(self) -> None:
        """Before, installed (plus the adapter cache) and after differ only in autonom-chatgpt@..."""
        self.write_personal()
        self.write_codex()
        self.make_cache("0.1.0", name=PROBE, plugin_dir=PROBE)
        before = self.status()["chatgpt"]
        self.install()
        self.make_cache("0.1.0")
        installed = self.status()["chatgpt"]
        self.assertEqual(installed["cache_plugins"], sorted(before["cache_plugins"] + [NAME + "@personal"]))
        self.assertEqual(installed["config_plugins"], before["config_plugins"])
        self.uninstall("--purge-cache")
        after = self.status()
        self.assertIs(after["installed"], False)
        self.assertEqual(after["chatgpt"], before)


class RefusalTests(InstallWorkspaceTestBase):
    def assert_untouched(self, before: dict[str, Any], state_before: dict[str, Any]) -> None:
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(snapshot(self.state), state_before)

    def test_invalid_marketplace_refused_without_write(self) -> None:
        self.market.parent.mkdir(parents=True)
        for text in ("{not json", "[1, 2]", '{"name": "personal"}', '{"plugins": {}}', "\xff"):
            with self.subTest(text=text):
                self.market.write_bytes(text.encode("latin-1"))
                before, state_before = snapshot(self.home), snapshot(self.state)
                code, result, _ = self.run_script("install")
                self.assert_refusal(code, result, "marketplace_invalid")
                self.assertEqual(result["path"], str(self.market))
                self.assert_untouched(before, state_before)
                code, result, _ = self.run_script("uninstall")
                self.assert_refusal(code, result, "marketplace_invalid")
                self.assert_untouched(before, state_before)

    def test_deeply_nested_json_files_are_refused_with_their_codes(self) -> None:
        deep = "[" * 200000
        self.market.parent.mkdir(parents=True)
        self.market.write_text(deep, encoding="utf-8")
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "marketplace_invalid")
        self.market.unlink()
        self.config.parent.mkdir(parents=True)
        self.config.write_text(deep, encoding="utf-8")
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "config_invalid")

    def test_unreadable_files_in_the_copy_or_source_are_reported_not_crashed(self) -> None:
        self.install()
        dangling = self.copy / "server" / "dangling.py"
        os.symlink(str(self.copy / "no-such-file"), str(dangling))
        code, result, err = self.run_script("status")
        self.assertEqual(code, 0, (result, err))
        self.assertIs(result["copy"]["stamp_ok"], False)
        self.assertIn("dangling.py", result["copy"]["reason"])
        self.assertIs(result["installed"], False)
        before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "write_failed")
        self.assertEqual(result["path"], str(dangling))
        self.assert_untouched(before, state_before)
        dangling.unlink()

        source = self.tmp / "broken-src" / NAME
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.symlink(str(source / "missing"), str(source / "server" / "dangling.py"))
        self.extra_env["AUTONOM_MCP_INSTALL_SOURCE"] = str(source)
        code, result, err = self.run_script("status")
        self.assertEqual(code, 0, (result, err))
        self.assertIs(result["copy"]["files_match_source"], False)
        self.assertIn("dangling.py", result["copy"]["source_reason"])
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "source_invalid")

    def test_foreign_entry_with_our_name_refused(self) -> None:
        self.market.parent.mkdir(parents=True)
        foreign = dict(ENTRY, source={"source": "local", "path": "./plugins/somewhere-else"})
        self.market.write_text(json.dumps({"name": "personal", "plugins": [PROBE_ENTRY, foreign]}),
                               encoding="utf-8")
        before, state_before = snapshot(self.home), snapshot(self.state)
        for command in ("install", "uninstall"):
            with self.subTest(command=command):
                code, result, _ = self.run_script(command)
                self.assert_refusal(code, result, "entry_conflict")
                self.assertEqual(result["found_path"], "./plugins/somewhere-else")
                self.assert_untouched(before, state_before)
        self.market.write_text(json.dumps({"plugins": [dict(ENTRY, source="./x")]}), encoding="utf-8")
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "entry_conflict")
        self.assertIsNone(result["found_path"])

    def test_probe_entry_is_never_mistaken_for_ours(self) -> None:
        # Only the exact name counts; a probe-only marketplace is left alone by uninstall.
        before = self.write_personal()
        result = self.uninstall()
        self.assertIs(result["changed"], False)
        self.assertEqual(self.market.read_bytes(), before)

    def test_foreign_plugin_dir_refused(self) -> None:
        self.copy.mkdir(parents=True)
        (self.copy / "mine.txt").write_text("user data", encoding="utf-8")
        before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "target_conflict")
        self.assertEqual(result["path"], str(self.copy))
        self.assert_untouched(before, state_before)
        # The probe's stamp (or any other plugin's) is foreign as well.
        (self.copy / ".autonom-probe-install.json").write_text(json.dumps({"plugin": PROBE}),
                                                                encoding="utf-8")
        (self.copy / STAMP).write_text(json.dumps({"plugin": PROBE}), encoding="utf-8")
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "target_conflict")
        before = snapshot(self.home)
        result = self.uninstall()
        self.assertIs(result["changed"], False)
        self.assertEqual(result["kept"][0]["path"], str(self.copy))
        self.assertEqual(snapshot(self.home), before)

    def test_symlinked_plugin_dir_refused(self) -> None:
        target = self.home / "elsewhere"
        target.mkdir()
        (self.home / "plugins").mkdir()
        os.symlink(target, self.copy)
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "target_conflict")
        self.assertTrue(self.copy.is_symlink())

    def test_invalid_flag_values_are_usage_errors(self) -> None:
        a_file = self.tmp / "not-a-dir"
        a_file.write_text("x", encoding="utf-8")
        roots = []
        for index in range(9):
            path = self.tmp / ("root-%d" % index)
            path.mkdir()
            roots += ["--flow-root", str(path)]
        fake_cli = self.tmp / "scripts" / "other.py"
        fake_cli.parent.mkdir()
        fake_cli.write_text("x", encoding="utf-8")
        missing_cli = self.tmp / "gone" / "scripts" / "autonom.py"
        cases = [
            (["--flow-root", "relative/flows"], "--flow-root"),
            (["--flow-root", str(self.tmp / "missing")], "--flow-root"),
            (["--flow-root", str(a_file)], "--flow-root"),
            (["--flow-root", ""], "--flow-root"),
            (roots, "--flow-root"),
            (["--allow-target", "android:"], "--allow-target"),
            (["--allow-target", "emulator-5584"], "--allow-target"),
            (["--allow-target", "windows:pc"], "--allow-target"),
            (["--allow-target", "ios:" + "A" * 129], "--allow-target"),
            ([arg for n in range(33) for arg in ("--allow-target", "android:emu-%d" % n)],
             "--allow-target"),
            (["--canvas-workspace", ""], "--canvas-workspace"),
            (["--canvas-workspace", "has space"], "--canvas-workspace"),
            (["--canvas-workspace", "x" * 41], "--canvas-workspace"),
            (["--control-idle", "14"], "--control-idle"),
            (["--control-idle", "3601"], "--control-idle"),
            (["--autonom-cli", "scripts/autonom.py"], "--autonom-cli"),
            (["--autonom-cli", str(missing_cli)], "--autonom-cli"),
            (["--autonom-cli", str(fake_cli)], "--autonom-cli"),
            (["--autonom-cli", str(ROOT / "scripts")], "--autonom-cli"),
        ]
        before, state_before = snapshot(self.home), snapshot(self.state)
        for flags, field in cases:
            with self.subTest(flags=flags[:4]):
                code, result, _ = self.run_script("install", *flags)
                self.assert_refusal(code, result, "invalid_arguments", exit_code=2)
                self.assertEqual(result["field"], field)
                self.assertIsInstance(result["problem"], str)
        self.assert_untouched(before, state_before)
        # argparse usage errors exit 2 too, with one JSON refusal under --json and usage on stderr.
        code, out, err = self.run_script("install", "--control-idle", "abc")
        self.assert_refusal(code, out, "invalid_arguments", exit_code=2)
        self.assertIn("--control-idle", out["error"])
        self.assertIn("usage", err)
        code, out, err = self.run_script("status", "--purge-cache")
        self.assert_refusal(code, out, "invalid_arguments", exit_code=2)
        code, out, err = self.run_script("status", "--purge-cache", json_out=False)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage", err)

    def test_autonom_cli_refusals_name_the_problem(self) -> None:
        code, result, _ = self.run_script("install", "--autonom-cli", "autonom.py")
        self.assert_refusal(code, result, "invalid_arguments", exit_code=2)
        self.assertIn("absolute", result["problem"])
        code, result, _ = self.run_script("install", "--autonom-cli",
                                          str(self.tmp / "nowhere" / "scripts" / "autonom.py"))
        self.assert_refusal(code, result, "invalid_arguments", exit_code=2)
        self.assertIn("existing", result["problem"])
        self.assertFalse(self.config.exists())

    def test_invalid_config_refused(self) -> None:
        self.config.parent.mkdir(parents=True)
        for text in ("{bad", "[]", '"text"'):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                before, state_before = snapshot(self.home), snapshot(self.state)
                code, result, _ = self.run_script("install")
                self.assert_refusal(code, result, "config_invalid")
                self.assertEqual(result["path"], str(self.config))
                self.assert_untouched(before, state_before)
        status = self.status()
        self.assertIs(status["config"]["valid"], False)

    def test_home_and_source_refusals(self) -> None:
        missing = self.home / "missing"
        for command in ("install", "uninstall", "status"):
            with self.subTest(command=command):
                code, result, _ = self.run_script(command, home=missing)
                self.assert_refusal(code, result, "home_invalid")
                self.assertEqual(result["path"], str(missing))
        self.assertFalse(missing.exists())
        empty = self.tmp / "empty-src"
        empty.mkdir()
        code, result, _ = self.run_script("install", env={"AUTONOM_MCP_INSTALL_SOURCE": str(empty)})
        self.assert_refusal(code, result, "source_invalid")
        (empty / ".codex-plugin").mkdir()
        for manifest in ('{"name": "autonom-chatgpt-probe", "version": "0.1.0"}',
                         '{"name": "autonom-chatgpt"}', "[]"):
            (empty / ".codex-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
            code, result, _ = self.run_script("install", env={"AUTONOM_MCP_INSTALL_SOURCE": str(empty)})
            self.assert_refusal(code, result, "source_invalid")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_copied_script_without_its_neighbours_is_refused(self) -> None:
        lone = self.tmp / "lone" / "scripts"
        lone.mkdir(parents=True)
        shutil.copy2(SCRIPT, lone / SCRIPT.name)
        code, result, _ = self.run_script("status", script=lone / SCRIPT.name)
        self.assert_refusal(code, result, "source_invalid")
        shutil.copy2(PROBE_SCRIPT, lone / PROBE_SCRIPT.name)
        code, result, _ = self.run_script("install", script=lone / SCRIPT.name)
        self.assert_refusal(code, result, "source_invalid")
        self.assertEqual(list(self.home.iterdir()), [])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
    def test_write_failure_is_reported(self) -> None:
        self.write_personal()
        os.chmod(self.market.parent, 0o500)
        self.addCleanup(os.chmod, self.market.parent, 0o755)
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "write_failed")
        self.assertEqual(result["path"], str(self.market))
        self.assertIsInstance(result["actions"], list)
        self.assertEqual(self.market.read_text(encoding="utf-8"), PERSONAL)

    def test_unreadable_marketplace_refused(self) -> None:
        self.market.mkdir(parents=True)  # a directory where the file should be
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "marketplace_invalid")
        self.assertIn("cannot be read", result["error"])
        self.assertFalse(os.path.lexists(self.copy))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
    def test_uninstall_write_failure_is_reported(self) -> None:
        self.write_personal()
        self.install()
        os.chmod(self.market.parent, 0o500)
        self.addCleanup(os.chmod, self.market.parent, 0o755)
        code, result, _ = self.run_script("uninstall")
        self.assert_refusal(code, result, "write_failed")
        self.assertEqual(result["path"], str(self.market))
        self.assertTrue(self.copy.is_dir(), "nothing after the failed write is removed")


class UninstallTests(InstallWorkspaceTestBase):
    def test_uninstall_restores_marketplace_and_removes_copy_and_config(self) -> None:
        before = self.write_personal()
        self.install("--allow-target", "android:emulator-5584")
        result = self.uninstall()
        self.assertTrue(result["changed"])
        self.assertEqual(json.loads(self.market.read_bytes()), json.loads(before))
        self.assertFalse(os.path.lexists(self.copy))
        self.assertFalse(self.config.exists())
        backups = self.backup_files()
        self.assertEqual(len(backups), 2)
        contents = sorted((p.read_bytes() for p in backups), key=len)
        self.assertEqual(contents[0], before)
        installed = json.loads(contents[1])
        self.assertEqual([p["name"] for p in installed["plugins"]], ["delivery-harness", PROBE, NAME])
        self.assert_no_codex()

    def test_uninstall_deletes_marketplace_it_created(self) -> None:
        self.install()
        result = self.uninstall()
        self.assertEqual([a["op"] for a in result["actions"]], ["backup", "remove", "remove", "remove"])
        self.assertFalse(self.market.exists())
        self.assertFalse(os.path.lexists(self.copy))
        self.assertEqual(len(self.backup_files()), 1)

    def test_uninstall_keeps_created_marketplace_with_other_plugins(self) -> None:
        self.install()
        data = json.loads(self.market.read_text(encoding="utf-8"))
        data["plugins"].append(PROBE_ENTRY)
        self.market.write_text(json.dumps(data), encoding="utf-8")
        self.uninstall()
        self.assertEqual(json.loads(self.market.read_text(encoding="utf-8"))["plugins"], [PROBE_ENTRY])

    def test_uninstall_when_absent_is_unchanged(self) -> None:
        home_before, state_before = snapshot(self.home), snapshot(self.state)
        result = self.uninstall()
        self.assertIs(result["changed"], False)
        self.assertEqual(result["actions"], [])
        self.assertEqual(snapshot(self.home), home_before)
        self.write_personal()
        home_before = snapshot(self.home)
        result = self.uninstall("--purge-cache")
        self.assertIs(result["changed"], False)
        self.assertEqual(snapshot(self.home), home_before)
        self.assertEqual(snapshot(self.state), state_before)

    def test_uninstall_ignores_a_stamp_config_path_that_is_not_ours(self) -> None:
        self.install()
        stamp = self.stamp()
        for victim in (self.home / "important.json", self.state / "chatgpt-probe" / "config.json"):
            with self.subTest(victim=victim):
                victim.parent.mkdir(parents=True, exist_ok=True)
                victim.write_text("{}", encoding="utf-8")
                stamp["config_path"] = str(victim)
                (self.copy / STAMP).write_text(json.dumps(stamp), encoding="utf-8")
                code, result, _ = self.run_script("uninstall", "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn(str(victim), [k["path"] for k in result["kept"]])
                self.assertNotIn(str(victim), [a["path"] for a in result["actions"]])
        result = self.uninstall()
        self.assertTrue((self.home / "important.json").exists())
        self.assertTrue((self.state / "chatgpt-probe" / "config.json").exists())

    def test_purge_cache_removes_only_a_matching_cache(self) -> None:
        for versions in (["local"], ["0.1.0"], ["local", "0.1.0"]):
            with self.subTest(versions=versions):
                self.write_personal()
                self.install()
                for version in versions:
                    base = self.make_cache(version)
                probe_cache = self.make_cache("0.1.0", name=PROBE, plugin_dir=PROBE)
                result = self.uninstall("--purge-cache")
                self.assertIn(str(base), [a["path"] for a in result["actions"]])
                self.assertFalse(base.exists())
                self.assertTrue((probe_cache / "0.1.0" / ".codex-plugin" / "plugin.json").exists())
                shutil.rmtree(self.home / ".codex")
                shutil.rmtree(self.home / ".agents")
                shutil.rmtree(self.home / "plugins")

    def test_purge_cache_uses_the_marketplace_name(self) -> None:
        self.write_personal(json.dumps({"name": "mine", "plugins": []}) + "\n")
        self.install()
        base = self.make_cache("local", marketplace="mine")
        decoy = self.make_cache("local", marketplace="personal")
        self.uninstall("--purge-cache")
        self.assertFalse(base.exists())
        self.assertTrue(decoy.exists())

    def test_purge_cache_refuses_a_foreign_cache(self) -> None:
        self.write_personal()
        self.install()
        base = self.make_cache("local")
        self.make_cache("0.2.0", name=PROBE)
        before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, _ = self.run_script("uninstall", "--purge-cache")
        self.assert_refusal(code, result, "cache_conflict")
        self.assertTrue(result["path"].endswith("plugin.json"))
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(snapshot(self.state), state_before)
        # Files without any manifest are not ours either.
        shutil.rmtree(base)
        (base / "local").mkdir(parents=True)
        (base / "local" / "data.bin").write_bytes(b"x")
        code, result, _ = self.run_script("uninstall", "--purge-cache")
        self.assert_refusal(code, result, "cache_conflict")
        # A symlinked cache directory is refused, never followed.
        shutil.rmtree(base)
        target = self.home / "cache-target"
        target.mkdir()
        os.symlink(target, base)
        code, result, _ = self.run_script("uninstall", "--purge-cache")
        self.assert_refusal(code, result, "cache_conflict")
        self.assertTrue(target.exists())
        # Without --purge-cache the cache is left alone and uninstall proceeds.
        result = self.uninstall()
        self.assertTrue(result["changed"])
        self.assertTrue(base.is_symlink())


class RepositoryTests(unittest.TestCase):
    def test_script_has_shebang_and_is_executable(self) -> None:
        self.assertTrue(SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n"))
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_script_parses_on_python_3_9(self) -> None:
        import ast
        ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_plugin_is_not_in_the_repo_marketplaces(self) -> None:
        for path in (ROOT / ".agents" / "plugins" / "marketplace.json",
                     ROOT / "integrations" / "chatgpt" / ".agents" / "plugins" / "marketplace.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(NAME, [p.get("name") for p in data["plugins"]], path)

    def test_readme_section_covers_the_contract_topics(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertTrue(text.isascii(), "README must be plain English ASCII")
        self.assertEqual(text.count("## Autonom MCP adapter (development)"), 1)
        section = text.split("## Autonom MCP adapter (development)", 1)[1]
        for needle in ("install_workspace.py install", "install_workspace.py uninstall",
                       "install_workspace.py status", "--dry-run", "--purge-cache", "--autonom-cli",
                       "--flow-root", "--allow-target", "--canvas-workspace", "--control-idle",
                       "config_plugins", "cache_plugins", "config.toml", "flow_roots",
                       "allowed_targets", "canvas_workspace", "control_idle_s", "autonom_cli",
                       "get_frames", "send_input", "get_artifact", "list_sessions", "token",
                       "artifact", "manual cleanup", "Autonom (dev)", "autonom.mcp/v1",
                       "outcome_unknown"):
            self.assertIn(needle, section)
        # Every documented error code is one the script can emit.
        script = SCRIPT.read_text(encoding="utf-8")
        for code in ("marketplace_invalid", "entry_conflict", "target_conflict", "config_invalid",
                     "cache_conflict", "invalid_arguments", "home_invalid", "source_invalid",
                     "write_failed"):
            self.assertIn(code, section)
            self.assertIn('"%s"' % code, script)

    def test_readme_says_to_keep_the_chatgpt_canvas_workspace(self) -> None:
        # Contract 0.2 (DEC-026): with `default`, a user's plain `canvas serve --serial X`
        # would join the adapter's Canvas and the adapter's exit would stop it.
        text = README.read_text(encoding="utf-8")
        section = text.split("## Autonom MCP adapter (development)", 1)[1]
        flat = " ".join(section.split())
        self.assertIn("Keep `canvas_workspace` at its default `chatgpt`.", flat)
        self.assertIn("a plain `autonom canvas serve --serial X` uses the workspace `default`", flat)
        self.assertIn("the adapter's exit would stop that Canvas", flat)
        self.assertIn("`--bootable-only`", flat)

    def test_readme_keeps_the_probe_section_first(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Autonom probe for ChatGPT desktop (development only)\n"))
        self.assertLess(text.index("## Manual cleanup"), text.index("## Autonom MCP adapter (development)"))


if __name__ == "__main__":
    unittest.main()
