"""ChatGPT probe install / uninstall / status script (contract sections 6 and 8, install).

Every test runs ``integrations/chatgpt/scripts/install_probe.py`` as a real
subprocess with ``--home <temp dir>`` and a sandboxed ``AUTONOM_HOME``, so
nothing is ever written to the real ``~/.agents``, ``~/plugins``, ``~/.codex``
or ``~/.local/state/autonom``.
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
except ImportError:  # python3 -m unittest tests.test_chatgpt_probe_install
    from tests.env_isolation import EnvSandboxMixin

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "integrations" / "chatgpt" / "scripts" / "install_probe.py"
SOURCE = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt-probe"
DEV_MARKETPLACE = ROOT / "integrations" / "chatgpt" / ".agents" / "plugins" / "marketplace.json"
NAME = "autonom-chatgpt-probe"
STAMP = ".autonom-probe-install.json"
ENTRY = {
    "name": NAME,
    "source": {"source": "local", "path": "./plugins/autonom-chatgpt-probe"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
}
# The owner's personal marketplace on the development Mac, byte for byte in shape.
PERSONAL = (
    '{\n  "name": "personal",\n  "interface": {\n    "displayName": "Personal"\n  },\n'
    '  "plugins": [\n    {\n      "name": "delivery-harness",\n      "source": {\n'
    '        "source": "local",\n        "path": "./plugins/delivery-harness"\n      },\n'
    '      "policy": {\n        "installation": "AVAILABLE",\n        "authentication": "ON_INSTALL"\n'
    '      },\n      "category": "Productivity"\n    }\n  ]\n}\n'
)
NEXT_STEPS = ("Restart ChatGPT desktop, open Plugins, choose the Personal marketplace, "
              "install Autonom probe, enable it in Work mode.")


def snapshot(root: Path) -> dict[str, Any]:
    """Every path under ``root`` with its type, mode and bytes."""
    out: dict[str, Any] = {}
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
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class InstallProbeTestBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.state = self.sandbox_home()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.market = self.home / ".agents" / "plugins" / "marketplace.json"
        self.copy = self.home / "plugins" / NAME
        self.backups = self.home / "plugins" / "backups" / NAME
        self.config = self.state / "chatgpt-probe" / "config.json"
        self.extra_env: dict[str, str] = {}

    # -- helpers

    def run_script(self, *args: str, home: Path | None = None, json_out: bool = True,
                   env: dict[str, str] | None = None) -> tuple[int, Any, str]:
        argv = [sys.executable, str(SCRIPT), *args, "--home", str(home or self.home)]
        if json_out:
            argv.append("--json")
        child_env = dict(os.environ)
        child_env["HOME"] = str(self.home)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env.pop("XDG_STATE_HOME", None)
        child_env.update(self.extra_env)
        child_env.update(env or {})
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=child_env, timeout=60, check=False)
        out = proc.stdout.decode("utf-8")
        if json_out and out.strip():
            lines = [line for line in out.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1, "expected one JSON line, got %r" % out)
            return proc.returncode, json.loads(lines[0]), proc.stderr.decode("utf-8")
        return proc.returncode, out, proc.stderr.decode("utf-8")

    def write_personal(self) -> bytes:
        self.market.parent.mkdir(parents=True)
        self.market.write_text(PERSONAL, encoding="utf-8")
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


class InstallTests(InstallProbeTestBase):
    def test_install_into_personal_marketplace_keeps_other_entries(self) -> None:
        before = self.write_personal()
        result = self.install("--allow-device", "android:emulator-5584",
                              "--remote-route", "forwarded=http://127.0.0.1:47812")
        self.assertTrue(result["changed"])
        self.assertEqual(result["next_steps"], NEXT_STEPS)
        self.assertEqual([a["op"] for a in result["actions"]],
                         ["copy", "backup", "write_marketplace", "write_config"])

        data = json.loads(self.market.read_text(encoding="utf-8"))
        original = json.loads(before)
        self.assertEqual(list(data), list(original))  # top-level key order
        self.assertEqual(data["name"], "personal")
        self.assertEqual(data["interface"], original["interface"])
        self.assertEqual(data["plugins"][0], original["plugins"][0])
        self.assertEqual(list(data["plugins"][0]), list(original["plugins"][0]))
        self.assertEqual(data["plugins"][1], ENTRY)
        self.assertEqual(len(data["plugins"]), 2)
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
            "remote_routes": [{"label": "forwarded", "origin": "http://127.0.0.1:47812"}],
            "allowed_devices": ["android:emulator-5584"]})
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        self.assertFalse(self.copy.is_symlink())
        self.assertTrue(os.access(self.copy / "bin" / "launch.sh", os.X_OK))
        self.assert_no_codex()
        # Only these top-level trees exist under the temp HOME.
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".agents", "plugins"])
        self.assertEqual(sorted(p.name for p in (self.home / "plugins").iterdir()),
                         sorted(["backups", NAME]))

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
        self.install("--media-port", "47900")
        first = self.stamp()
        backups = self.backup_files()
        market_bytes = self.market.read_bytes()

        result = self.install("--media-port", "47900")
        self.assertIs(result["changed"], False)
        self.assertEqual(result["actions"], [])
        self.assertEqual(self.backup_files(), backups)
        self.assertEqual(self.market.read_bytes(), market_bytes)
        self.assertEqual(self.stamp(), first)

        # A flag change rewrites the config only; the stamp keeps wrote_config.
        result = self.install("--media-port", "47901")
        self.assertEqual([a["op"] for a in result["actions"]], ["write_config"])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["media_port"], 47901)
        self.assertIs(self.stamp()["wrote_config"], True)

        # A changed entry is replaced in place, after a new backup; the first backup stays first.
        data = json.loads(market_bytes)
        data["plugins"][1]["category"] = "Other"
        data["plugins"].append({"name": "later", "source": {"source": "local", "path": "./plugins/later"}})
        self.market.write_text(json.dumps(data), encoding="utf-8")
        edited = self.market.read_bytes()
        result = self.install("--media-port", "47901")
        self.assertEqual([a["op"] for a in result["actions"]], ["backup", "write_marketplace"])
        new = json.loads(self.market.read_text(encoding="utf-8"))
        self.assertEqual([p["name"] for p in new["plugins"]], ["delivery-harness", NAME, "later"])
        self.assertEqual(new["plugins"][1], ENTRY)
        backups_now = self.backup_files()
        self.assertEqual(len(backups_now), len(backups) + 1)
        self.assertIn(edited, [p.read_bytes() for p in backups_now])
        self.assertEqual(self.stamp()["first_backup"], first["first_backup"])

    def test_created_marketplace_and_wrote_config_survive_a_reinstall(self) -> None:
        self.install()
        # Touch the source copy so the next install refreshes the copy and rewrites the stamp.
        (self.copy / "bin" / "launch.sh").write_text("changed\n", encoding="utf-8")
        result = self.install("--allow-origin", "https://Example.COM")
        self.assertIn("copy", [a["op"] for a in result["actions"]])
        stamp = self.stamp()
        self.assertIs(stamp["created_marketplace"], True)
        self.assertIs(stamp["wrote_config"], True)
        self.assertEqual((self.copy / "bin" / "launch.sh").read_bytes(),
                         (SOURCE / "bin" / "launch.sh").read_bytes())
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")),
                         {"allow_origins": ["https://example.com"]})
        self.assertEqual(json.loads(self.market.read_text(encoding="utf-8"))["plugins"], [ENTRY])

    def test_duplicate_own_entries_collapse_to_one(self) -> None:
        self.market.parent.mkdir(parents=True)
        self.market.write_text(json.dumps({"name": "personal", "plugins": [
            dict(ENTRY, category="A"), {"name": "x"}, dict(ENTRY, category="B")]}), encoding="utf-8")
        self.install()
        plugins = json.loads(self.market.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual(plugins, [ENTRY, {"name": "x"}])

    def test_dry_run_writes_nothing(self) -> None:
        self.write_personal()
        state_before = snapshot(self.state)
        before = snapshot(self.home)
        code, result, _ = self.run_script("install", "--dry-run", "--allow-device", "ios:" + "A" * 36)
        self.assertEqual(code, 0, result)
        self.assertIs(result["dry_run"], True)
        self.assertEqual([a["op"] for a in result["actions"]],
                         ["copy", "backup", "write_marketplace", "write_config"])
        for action in result["actions"]:
            self.assertTrue(os.path.isabs(action["path"]))
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(snapshot(self.state), state_before)

        # Also against an installed tree, for uninstall with --purge-cache.
        self.install()
        self.make_cache("local")
        before = snapshot(self.home)
        state_before = snapshot(self.state)
        code, result, _ = self.run_script("uninstall", "--dry-run", "--purge-cache")
        self.assertEqual(code, 0, result)
        self.assertEqual([a["op"] for a in result["actions"]],
                         ["backup", "write_marketplace", "remove", "remove", "remove"])
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(snapshot(self.state), state_before)

    def make_cache(self, version: str, name: str = NAME, marketplace: str = "personal") -> Path:
        base = self.home / ".codex" / "plugins" / "cache" / marketplace / NAME
        manifest = base / version / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8")
        (base / version / "server").mkdir()
        (base / version / "server" / "probe_server.py").write_text("# cached\n", encoding="utf-8")
        return base

    def test_copy_excludes_caches_and_matches_source(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        source = tmp / NAME
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (source / "server" / "__pycache__").mkdir()
        (source / "server" / "__pycache__" / "probe_env.cpython-314.pyc").write_bytes(b"\0")
        (source / "server" / "stray.pyc").write_bytes(b"\0")
        (source / ".DS_Store").write_bytes(b"\0")
        (source / "bin" / ".DS_Store").write_bytes(b"\0")
        self.extra_env["AUTONOM_PROBE_INSTALL_SOURCE"] = str(source)
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
        self.config.write_text(json.dumps({"cli_timeout_s": 30, "media_port": 47811,
                                           "allowed_devices": ["android:old-1"]}), encoding="utf-8")
        result = self.install("--media-port", "48000", "--allow-device", "android:emulator-5584",
                              "--allow-device", "android:emulator-5584",
                              "--allow-origin", "https://*.web-sandbox.oaiusercontent.com")
        self.assertIn("write_config", [a["op"] for a in result["actions"]])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {
            "cli_timeout_s": 30, "media_port": 48000, "allowed_devices": ["android:emulator-5584"],
            "allow_origins": ["https://*.web-sandbox.oaiusercontent.com"]})
        self.assertIs(self.stamp()["wrote_config"], False)
        # Uninstall keeps a config that install did not create.
        result = self.uninstall()
        self.assertTrue(self.config.exists())
        self.assertIn(str(self.config), [k["path"] for k in result["kept"]])

    def test_status_fields(self) -> None:
        self.write_personal()
        code, status, _ = self.run_script("status")
        self.assertEqual(code, 0)
        self.assertIs(status["ok"], True)
        self.assertIs(status["installed"], False)
        self.assertIsNone(status["entry"])
        self.assertEqual(status["copy"]["path"], str(self.copy))
        self.assertIs(status["copy"]["stamp_ok"], False)
        self.assertEqual(status["marketplace"]["path"], str(self.market))
        self.assertEqual(status["marketplace"]["plugins"], ["delivery-harness"])
        self.assertIs(status["cache"]["present"], False)
        self.assertEqual(status["cache"]["path"],
                         str(self.home / ".codex" / "plugins" / "cache" / "personal" / NAME))
        self.assertIs(status["config"]["exists"], False)
        self.assertEqual(status["backups"], [])

        self.install("--media-port", "47999")
        self.make_cache("0.1.0")
        code, status, _ = self.run_script("status")
        self.assertIs(status["installed"], True)
        self.assertEqual(status["entry"], ENTRY)
        self.assertIs(status["copy"]["stamp_ok"], True)
        self.assertIs(status["copy"]["files_match_source"], True)
        self.assertEqual(status["copy"]["version"], "0.1.0")
        self.assertEqual(status["marketplace"]["plugins"], ["delivery-harness", NAME])
        self.assertEqual(status["cache"], {"present": True, "versions": ["0.1.0"],
                                           "path": status["cache"]["path"]})
        self.assertEqual(status["config"]["values"], {"media_port": 47999})
        self.assertEqual(status["config"]["path"], str(self.config))
        self.assertEqual(len(status["backups"]), 1)

        # A tampered copy shows up as stale; never prints other plugins' files.
        (self.copy / "bin" / "launch.sh").write_text("x\n", encoding="utf-8")
        code, status, _ = self.run_script("status")
        self.assertIs(status["copy"]["stamp_ok"], False)
        self.assertIs(status["copy"]["files_match_source"], False)
        self.assertIs(status["installed"], False)

        # Invalid marketplace: status still answers, flagged invalid.
        self.market.write_text("{nope", encoding="utf-8")
        code, status, _ = self.run_script("status")
        self.assertEqual(code, 0)
        self.assertIs(status["marketplace"]["valid"], False)
        self.assertEqual(status["marketplace"]["error_code"], "marketplace_invalid")

    def test_human_output(self) -> None:
        code, out, _ = self.run_script("install", json_out=False)
        self.assertEqual(code, 0)
        self.assertIn("Done: copy", out)
        self.assertIn(NEXT_STEPS, out)
        code, out, _ = self.run_script("status", json_out=False)
        self.assertEqual(code, 0)
        self.assertIn("Autonom probe installed: yes", out)
        code, out, err = self.run_script("install", "--allow-device", "android:emulator-5554",
                                         json_out=False)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("device_not_allowed", err)
        self.assertIn("hint:", err)


class RefusalTests(InstallProbeTestBase):
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
        code, result, err = self.run_script("install")
        self.assert_refusal(code, result, "marketplace_invalid")
        self.assertNotIn("Traceback", err)
        self.market.unlink()
        self.config.parent.mkdir(parents=True)
        self.config.write_text(deep, encoding="utf-8")
        code, result, err = self.run_script("install")
        self.assert_refusal(code, result, "config_invalid")
        self.assertNotIn("Traceback", err)

    def test_unreadable_files_in_the_copy_or_source_are_reported_not_crashed(self) -> None:
        self.install()
        dangling = self.copy / "server" / "dangling.py"
        os.symlink(str(self.copy / "no-such-file"), str(dangling))
        code, result, err = self.run_script("status")
        self.assertEqual(code, 0, (result, err))
        self.assertIs(result["copy"]["stamp_ok"], False)
        self.assertIs(result["copy"]["files_match_source"], False)
        self.assertIn("dangling.py", result["copy"]["reason"])
        self.assertIs(result["installed"], False)
        before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, err = self.run_script("install")
        self.assert_refusal(code, result, "write_failed")
        self.assertEqual(result["path"], str(dangling))
        self.assertNotIn("Traceback", err)
        self.assert_untouched(before, state_before)
        dangling.unlink()

        locked = self.copy / "server" / "locked.py"
        locked.write_text("x\n", encoding="utf-8")
        locked.chmod(0)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o644))
        if not os.access(str(locked), os.R_OK):  # root can read anything
            code, result, err = self.run_script("status")
            self.assertEqual(code, 0, (result, err))
            self.assertIs(result["copy"]["stamp_ok"], False)
            self.assertIn("locked.py", result["copy"]["reason"])
            code, result, err = self.run_script("install")
            self.assert_refusal(code, result, "write_failed")
        locked.chmod(0o644)
        locked.unlink()

        source = Path(tempfile.mkdtemp(prefix="probe-source-"))
        self.addCleanup(shutil.rmtree, source, True)
        source = source / NAME
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.symlink(str(source / "missing"), str(source / "server" / "dangling.py"))
        self.extra_env["AUTONOM_PROBE_INSTALL_SOURCE"] = str(source)
        code, result, err = self.run_script("status")
        self.assertEqual(code, 0, (result, err))
        self.assertIs(result["copy"]["files_match_source"], False)
        self.assertIn("dangling.py", result["copy"]["source_reason"])
        code, result, err = self.run_script("install")
        self.assert_refusal(code, result, "source_invalid")
        self.assertNotIn("Traceback", err)

    def test_foreign_entry_with_our_name_refused(self) -> None:
        self.market.parent.mkdir(parents=True)
        foreign = dict(ENTRY, source={"source": "local", "path": "./plugins/somewhere-else"})
        self.market.write_text(json.dumps({"name": "personal", "plugins": [foreign]}), encoding="utf-8")
        before, state_before = snapshot(self.home), snapshot(self.state)
        for command in ("install", "uninstall"):
            with self.subTest(command=command):
                code, result, _ = self.run_script(command)
                self.assert_refusal(code, result, "entry_conflict")
                self.assertEqual(result["found_path"], "./plugins/somewhere-else")
                self.assert_untouched(before, state_before)
        # A string source with our name is foreign too.
        self.market.write_text(json.dumps({"plugins": [dict(ENTRY, source="./x")]}), encoding="utf-8")
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "entry_conflict")
        self.assertIsNone(result["found_path"])

    def test_foreign_plugin_dir_refused(self) -> None:
        self.copy.mkdir(parents=True)
        (self.copy / "mine.txt").write_text("user data", encoding="utf-8")
        before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "target_conflict")
        self.assertEqual(result["path"], str(self.copy))
        self.assert_untouched(before, state_before)
        # A stamp naming another plugin is foreign as well.
        (self.copy / STAMP).write_text(json.dumps({"plugin": "other"}), encoding="utf-8")
        code, result, _ = self.run_script("install")
        self.assert_refusal(code, result, "target_conflict")
        # Uninstall leaves the foreign directory alone and says so.
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

    def test_emulator_5554_refused(self) -> None:
        before, state_before = snapshot(self.home), snapshot(self.state)
        code, result, _ = self.run_script("install", "--allow-device", "android:emulator-5584",
                                          "--allow-device", "android:emulator-5554")
        self.assert_refusal(code, result, "device_not_allowed")
        self.assertEqual(result["field"], "--allow-device")
        self.assert_untouched(before, state_before)

    def test_invalid_flag_values_are_usage_errors(self) -> None:
        cases = [
            (["--media-port", "80"], "--media-port"),
            (["--media-port", "70000"], "--media-port"),
            (["--remote-route", "no-equals"], "--remote-route"),
            (["--remote-route", "=http://127.0.0.1:1"], "--remote-route"),
            (["--remote-route", "x" * 41 + "=http://127.0.0.1:47812"], "--remote-route"),
            (["--remote-route", "a=http://example.com"], "--remote-route"),
            (["--remote-route", "a=http://127.0.0.1:47812", "--remote-route", "a=http://127.0.0.1:47813"],
             "--remote-route"),
            ([arg for n in range(5) for arg in ("--remote-route", "r%d=http://127.0.0.1:4781%d" % (n, n))],
             "--remote-route"),
            (["--allow-origin", "null"], "--allow-origin"),
            (["--allow-origin", "https://*.com"], "--allow-origin"),
            (["--allow-origin", "https://a.example/path"], "--allow-origin"),
            (["--allow-device", "android:"], "--allow-device"),
            (["--allow-device", "ios:not-a-udid"], "--allow-device"),
            (["--allow-device", "emulator-5584"], "--allow-device"),
        ]
        before, state_before = snapshot(self.home), snapshot(self.state)
        for flags, field in cases:
            with self.subTest(flags=flags):
                code, result, _ = self.run_script("install", *flags)
                self.assert_refusal(code, result, "invalid_arguments", exit_code=2)
                self.assertEqual(result["field"], field)
                self.assertIsInstance(result["problem"], str)
        self.assert_untouched(before, state_before)
        # argparse usage errors exit 2 too, with one JSON refusal under --json and usage on stderr.
        code, out, err = self.run_script("install", "--media-port", "abc")
        self.assert_refusal(code, out, "invalid_arguments", exit_code=2)
        self.assertIn("--media-port", out["error"])
        self.assertIn("usage", err)
        code, out, err = self.run_script("status", "--purge-cache")
        self.assert_refusal(code, out, "invalid_arguments", exit_code=2)
        code, out, err = self.run_script("status", "--purge-cache", json_out=False)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage", err)

    def test_invalid_config_refused(self) -> None:
        self.config.parent.mkdir(parents=True)
        for text in ("{bad", "[]"):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                before, state_before = snapshot(self.home), snapshot(self.state)
                code, result, _ = self.run_script("install")
                self.assert_refusal(code, result, "config_invalid")
                self.assertEqual(result["path"], str(self.config))
                self.assert_untouched(before, state_before)

    def test_home_and_source_refusals(self) -> None:
        missing = self.home / "missing"
        for command in ("install", "uninstall", "status"):
            with self.subTest(command=command):
                code, result, _ = self.run_script(command, home=missing)
                self.assert_refusal(code, result, "home_invalid")
                self.assertEqual(result["path"], str(missing))
        self.assertFalse(missing.exists())
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        code, result, _ = self.run_script("install",
                                          env={"AUTONOM_PROBE_INSTALL_SOURCE": str(empty)})
        self.assert_refusal(code, result, "source_invalid")
        (empty / ".codex-plugin").mkdir()
        (empty / ".codex-plugin" / "plugin.json").write_text('{"name": "other", "version": "1"}',
                                                             encoding="utf-8")
        code, result, _ = self.run_script("install",
                                          env={"AUTONOM_PROBE_INSTALL_SOURCE": str(empty)})
        self.assert_refusal(code, result, "source_invalid")
        self.assertEqual(list(self.home.iterdir()), [])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
    def test_write_failure_is_reported(self) -> None:
        self.write_personal()
        plugins_dir = self.market.parent
        os.chmod(plugins_dir, 0o500)
        self.addCleanup(os.chmod, plugins_dir, 0o755)
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


class UninstallTests(InstallProbeTestBase):
    def test_uninstall_restores_marketplace_and_removes_copy_and_config(self) -> None:
        before = self.write_personal()
        self.install("--allow-device", "android:emulator-5584")
        self.assertTrue(self.config.exists())
        result = self.uninstall()
        self.assertTrue(result["changed"])
        self.assertIn("config.toml", result["manual_steps"])
        self.assertEqual(json.loads(self.market.read_bytes()), json.loads(before))
        self.assertFalse(os.path.lexists(self.copy))
        self.assertFalse(self.config.exists())
        backups = self.backup_files()
        self.assertEqual(len(backups), 2)
        contents = sorted((p.read_bytes() for p in backups), key=len)
        self.assertEqual(contents[0], before)
        installed = json.loads(contents[1])
        self.assertEqual([p["name"] for p in installed["plugins"]], ["delivery-harness", NAME])
        self.assert_no_codex()
        # Nothing but the backups is left behind.
        self.assertEqual(sorted(p.name for p in (self.home / "plugins").iterdir()), ["backups"])

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
        data["plugins"].append({"name": "later"})
        self.market.write_text(json.dumps(data), encoding="utf-8")
        self.uninstall()
        self.assertEqual(json.loads(self.market.read_text(encoding="utf-8"))["plugins"],
                         [{"name": "later"}])

    def test_uninstall_when_absent_is_unchanged(self) -> None:
        before, state_before = snapshot(self.home), snapshot(self.state)
        result = self.uninstall()
        self.assertIs(result["changed"], False)
        self.assertEqual(result["actions"], [])
        self.write_personal()
        before = snapshot(self.home)
        result = self.uninstall("--purge-cache")
        self.assertIs(result["changed"], False)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(snapshot(self.state), state_before)

    def test_uninstall_ignores_a_stamp_config_path_that_is_not_ours(self) -> None:
        self.install()
        stamp = self.stamp()
        victim = self.home / "important.json"
        victim.write_text("{}", encoding="utf-8")
        stamp["config_path"] = str(victim)
        (self.copy / STAMP).write_text(json.dumps(stamp), encoding="utf-8")
        result = self.uninstall()
        self.assertTrue(victim.exists())
        self.assertIn(str(victim), [k["path"] for k in result["kept"]])

    def make_cache(self, version: str, name: str = NAME) -> Path:
        base = self.home / ".codex" / "plugins" / "cache" / "personal" / NAME
        manifest = base / version / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8")
        return base

    def test_purge_cache_removes_only_a_matching_cache(self) -> None:
        for versions in (["local"], ["0.1.0"], ["local", "0.1.0"]):
            with self.subTest(versions=versions):
                self.write_personal()
                self.install()
                for version in versions:
                    base = self.make_cache(version)
                other = self.home / ".codex" / "plugins" / "cache" / "personal" / "delivery-harness"
                other.mkdir(parents=True)
                (other / "keep.txt").write_text("x", encoding="utf-8")
                result = self.uninstall("--purge-cache")
                self.assertIn(str(base), [a["path"] for a in result["actions"]])
                self.assertFalse(base.exists())
                self.assertTrue((other / "keep.txt").exists())
                shutil.rmtree(self.home / ".codex")
                shutil.rmtree(self.home / ".agents")
                shutil.rmtree(self.home / "plugins")

    def test_purge_cache_refuses_a_foreign_cache(self) -> None:
        self.write_personal()
        self.install()
        base = self.make_cache("local")
        self.make_cache("0.2.0", name="someone-else")
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
    def test_dev_marketplace_shape(self) -> None:
        data = json.loads(DEV_MARKETPLACE.read_text(encoding="utf-8"))
        self.assertEqual(data, {
            "name": "autonom-dev", "interface": {"displayName": "Autonom dev"},
            "plugins": [{"name": NAME, "source": {"source": "local", "path": "./autonom-chatgpt-probe"},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                         "category": "Developer Tools"}]})
        self.assertTrue((DEV_MARKETPLACE.parents[2] / "autonom-chatgpt-probe" / ".codex-plugin"
                         / "plugin.json").is_file())

    def test_script_has_shebang_and_is_executable(self) -> None:
        self.assertTrue(SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n"))
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_root_marketplaces_and_validator_unaffected(self) -> None:
        root_market = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json")
                                 .read_text(encoding="utf-8"))
        self.assertNotIn(NAME, [p.get("name") for p in root_market["plugins"]])
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_plugin.py"), str(ROOT)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertEqual(proc.stdout.decode("utf-8").strip(),
                         "Validated Codex and Claude manifests and 24 skills.")

    def test_readme_covers_the_contract_topics(self) -> None:
        text = (ROOT / "integrations" / "chatgpt" / "README.md").read_text(encoding="utf-8")
        for needle in ("install_probe.py install", "install_probe.py uninstall",
                       "install_probe.py status", "refused_origins", "--allow-origin",
                       "ssh -N -L 47812:127.0.0.1:47811", "--remote-route forwarded=http://127.0.0.1:47812",
                       "chatgpt_probe_host_live.mjs", "config.toml", "media_port", "remote_routes",
                       "allow_origins", "allowed_devices", "127.0.0.1", "Work mode"):
            self.assertIn(needle, text)
        self.assertTrue(text.isascii(), "README must be plain English ASCII")


if __name__ == "__main__":
    unittest.main()
