"""Stable host id and `autonom host info` (run ws-p3-mcp-adapter, contract 1).

- the first call mints `<state base>/host.json` (0600, schema 1) and prints
  its id; a second call and a second process print the same id;
- 16 processes released at the same moment print one id and leave one file;
- a corrupt, foreign or empty file is refused with `host_identity_invalid`
  (exit 2, a hint naming the file) and stays byte for byte as it was;
- with `AUTONOM_HOME` unset the file lives in `$XDG_STATE_HOME/autonom/`;
- the printed keys are exactly the contract's; the verb is READONLY and
  documented.

Every CLI run is a subprocess of the real CLI with `HOME`, `AUTONOM_HOME` and
`XDG_STATE_HOME` in a temporary directory and failing `adb`, `emulator`,
`xcrun`, `simctl` and `idb` stubs first on its `PATH`; each test asserts that
no stub was called. No device, no host service.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
DOC = ROOT / "docs/CAPABILITIES.md"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import __version__  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import host as host_mod  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

HOST_ID = re.compile(r"^h_[a-z2-7]{26}$")
INFO_KEYS = {"ok", "host_id", "name", "autonom_version", "platform", "capabilities"}
CAPABILITIES = ["sessions", "control.lease", "canvas", "flows", "artifacts"]
DEVICE_TOOLS = ("adb", "emulator", "xcrun", "simctl", "idb")
CORRUPT = b'{"schema": 1, "host_id": "nope"}'

# A child that waits for a gate file, then runs the real CLI in-process, so
# that the racing processes reach the identity file at the same moment.
WAIT_THEN_RUN = """
import os, runpy, sys, time
gate, cli = sys.argv[1], sys.argv[2]
deadline = time.monotonic() + 60
while not os.path.exists(gate):
    if time.monotonic() > deadline:
        sys.exit(98)
    time.sleep(0.002)
sys.argv = [cli, *sys.argv[3:]]
runpy.run_path(cli, run_name="__main__")
"""


class Sandbox:
    """A temporary HOME / AUTONOM_HOME / XDG_STATE_HOME and a stub PATH."""

    def __init__(self, test: unittest.TestCase, *, autonom_home: bool = True) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.xdg = self.root / "xdg"
        self.autonom_home = self.root / "state" if autonom_home else None
        self.bin = self.root / "bin"
        self.calls = self.root / "device-tool-calls.log"
        self.home.mkdir()
        self.xdg.mkdir()
        self.bin.mkdir()
        for tool in DEVICE_TOOLS:
            stub = self.bin / tool
            stub.write_text(
                "#!/bin/sh\n"
                f"echo \"{tool} $*\" >> '{self.calls}'\n"
                f"echo '{tool} stub: device tools are not allowed in this test' >&2\n"
                "exit 97\n",
                encoding="utf-8")
            stub.chmod(0o755)

    @property
    def base(self) -> Path:
        return self.autonom_home if self.autonom_home is not None else self.xdg / "autonom"

    @property
    def host_file(self) -> Path:
        return self.base / "host.json"

    def env(self) -> dict[str, str]:
        env = {
            "PATH": os.pathsep.join([str(self.bin), "/usr/bin", "/bin"]),
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.xdg),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C",
        }
        if self.autonom_home is not None:
            env["AUTONOM_HOME"] = str(self.autonom_home)
        if os.environ.get("TMPDIR"):
            env["TMPDIR"] = os.environ["TMPDIR"]
        return env

    def run(self, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *argv], env=self.env(),
                              cwd=str(self.root), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def device_tool_calls(self) -> str:
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def tree(self) -> set[str]:
        """Every path under the sandbox root except the stub directory."""
        return {str(path.relative_to(self.root)) for path in self.root.rglob("*")
                if self.bin not in path.parents and path != self.bin}


def _json(stream: str) -> dict[str, Any]:
    value = json.loads(stream)
    assert isinstance(value, dict)
    return value


class HostInfoCliTests(unittest.TestCase):

    def assert_no_device_tool(self, box: Sandbox) -> None:
        self.assertEqual(box.device_tool_calls(), "", "a device tool stub was called")

    def info(self, box: Sandbox) -> dict[str, Any]:
        result = box.run("host", "info")
        self.assertEqual(result.returncode, 0, result.stderr)
        return _json(result.stdout)

    def test_first_and_later_calls_print_one_id_from_a_0600_file(self) -> None:
        box = Sandbox(self)
        self.assertFalse(box.base.exists())
        first = self.info(box)
        second = self.info(box)
        self.assertRegex(first["host_id"], HOST_ID)
        self.assertEqual(second["host_id"], first["host_id"])
        mode = stat.S_IMODE(box.host_file.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        record = json.loads(box.host_file.read_text(encoding="utf-8"))
        self.assertEqual(set(record), {"schema", "host_id", "created_at"})
        self.assertEqual(record["schema"], 1)
        self.assertEqual(record["host_id"], first["host_id"])
        self.assertRegex(record["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assert_no_device_tool(box)

    def test_a_second_process_reads_the_same_id_and_the_file_is_not_rewritten(self) -> None:
        box = Sandbox(self)
        first = self.info(box)
        before = box.host_file.read_bytes()
        stamp = box.host_file.stat().st_mtime_ns
        other = subprocess.run([sys.executable, str(CLI), "host", "info"], env=box.env(),
                               cwd=str(box.home), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertEqual(_json(other.stdout)["host_id"], first["host_id"])
        self.assertEqual(box.host_file.read_bytes(), before)
        self.assertEqual(box.host_file.stat().st_mtime_ns, stamp)
        self.assert_no_device_tool(box)

    def test_keys_are_exactly_the_contract_ones(self) -> None:
        box = Sandbox(self)
        payload = self.info(box)
        self.assertEqual(set(payload), INFO_KEYS)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["autonom_version"], __version__)
        self.assertEqual(payload["platform"], sys.platform)
        self.assertEqual(payload["capabilities"], CAPABILITIES)
        self.assertIsInstance(payload["name"], str)
        self.assertTrue(0 < len(payload["name"]) <= 80)
        self.assert_no_device_tool(box)

    def test_sixteen_racing_processes_print_one_id_and_leave_one_file(self) -> None:
        box = Sandbox(self)
        gate = box.root / "go"
        children = [
            subprocess.Popen([sys.executable, "-c", WAIT_THEN_RUN, str(gate), str(CLI),
                              "host", "info"],
                             env=box.env(), cwd=str(box.root), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(16)
        ]
        try:
            time.sleep(0.5)  # every child is past interpreter start-up and polling
            gate.write_text("go", encoding="utf-8")
            outputs = [child.communicate(timeout=120) for child in children]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        ids = set()
        for child, (stdout, stderr) in zip(children, outputs):
            self.assertEqual(child.returncode, 0, stderr)
            ids.add(_json(stdout)["host_id"])
        self.assertEqual(len(ids), 1, ids)
        (only,) = ids
        self.assertRegex(only, HOST_ID)
        self.assertEqual(json.loads(box.host_file.read_text(encoding="utf-8"))["host_id"], only)
        self.assertEqual(sorted(path.name for path in box.base.iterdir()
                                if path.name.startswith("host")), ["host.json"])
        self.assertEqual([path.name for path in box.base.iterdir() if path.is_file()],
                         ["host.json"])
        self.assert_no_device_tool(box)

    def test_corrupt_file_is_refused_and_left_unchanged(self) -> None:
        # What any verb creates in a fresh state base (the session store), so
        # that "no other file" means: nothing beyond that.
        reference = Sandbox(self)
        reference.base.mkdir(mode=0o700)
        version = reference.run("version")
        self.assertEqual(version.returncode, 0, version.stderr)
        baseline = {name.replace("state/", "", 1) for name in reference.tree()
                    if name.startswith("state/")}

        box = Sandbox(self)
        box.base.mkdir(mode=0o700)
        box.host_file.write_bytes(CORRUPT)
        os.chmod(box.host_file, 0o644)
        stamp = box.host_file.stat().st_mtime_ns
        before = box.tree()
        result = box.run("host", "info")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(result.stdout, "")
        envelope = _json(result.stderr)
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error_code"], "host_identity_invalid")
        self.assertIn(str(box.host_file), envelope["hint"])
        self.assertIn("Move", envelope["hint"])
        self.assertEqual(box.host_file.read_bytes(), CORRUPT)
        self.assertEqual(box.host_file.stat().st_mtime_ns, stamp)
        self.assertEqual(stat.S_IMODE(box.host_file.stat().st_mode), 0o644)
        created = box.tree() - before
        allowed = {f"state/{name}" for name in baseline}
        self.assertEqual(created - allowed, set(), "the refusal created files")
        self.assertEqual([path for path in box.base.rglob("*") if path.is_file()],
                         [box.host_file])
        self.assert_no_device_tool(box)

    def test_xdg_state_home_placement_when_autonom_home_is_unset(self) -> None:
        box = Sandbox(self, autonom_home=False)
        self.assertNotIn("AUTONOM_HOME", box.env())
        payload = self.info(box)
        expected = box.xdg / "autonom" / "host.json"
        self.assertTrue(expected.is_file())
        self.assertEqual(json.loads(expected.read_text(encoding="utf-8"))["host_id"],
                         payload["host_id"])
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
        # host.py created the base itself here: 0700.
        self.assertEqual(stat.S_IMODE(expected.parent.stat().st_mode), 0o700)
        self.assertFalse((box.home / ".local/state/autonom/host.json").exists())
        self.assert_no_device_tool(box)

    def test_bare_group_is_a_usage_error(self) -> None:
        box = Sandbox(self)
        result = box.run("host")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(_json(result.stderr)["error_code"], "usage_error")
        self.assertFalse(box.host_file.exists())
        self.assert_no_device_tool(box)


class HostModuleTests(EnvSandboxMixin, unittest.TestCase):
    """The library functions in-process, in a sandboxed environment."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / "state"
        self.set_env(HOME=str(self.root / "home"), AUTONOM_HOME=str(self.base),
                     XDG_STATE_HOME=str(self.root / "xdg"))
        self.path = self.base / "host.json"

    def write(self, data: bytes) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def assert_refused(self, data: bytes) -> errors.AutonomError:
        self.write(data)
        with mock.patch.object(host_mod, "_READ_RETRY_DELAY_S", 0):
            with self.assertRaises(errors.AutonomError) as caught:
                host_mod.host_id()
        self.assertEqual(caught.exception.code, errors.HOST_IDENTITY_INVALID)
        self.assertIn(str(self.path), caught.exception.hint or "")
        self.assertEqual(self.path.read_bytes(), data)
        return caught.exception

    def test_state_base_rules(self) -> None:
        self.assertEqual(host_mod.state_base(), self.base)
        self.set_env(AUTONOM_HOME=None)
        self.assertEqual(host_mod.state_base(), self.root / "xdg" / "autonom")
        self.set_env(XDG_STATE_HOME=None)
        self.assertEqual(host_mod.state_base(), self.root / "home" / ".local/state/autonom")

    def test_new_ids_match_the_pattern_and_differ(self) -> None:
        minted = {host_mod._new_host_id() for _ in range(200)}  # noqa: SLF001
        self.assertEqual(len(minted), 200)
        for value in minted:
            self.assertRegex(value, HOST_ID)

    def test_invalid_files_are_refused_unchanged(self) -> None:
        cases = {
            "pattern": CORRUPT,
            "not json": b"{not json",
            "not utf-8": b"\xff\xfe\x00",
            "not an object": b"[1, 2]",
            "other schema": b'{"schema": 2, "host_id": "h_aaaaaaaaaaaaaaaaaaaaaaaaaa"}',
            "bool schema": b'{"schema": true, "host_id": "h_aaaaaaaaaaaaaaaaaaaaaaaaaa"}',
            "no schema": b'{"host_id": "h_aaaaaaaaaaaaaaaaaaaaaaaaaa"}',
            "upper case id": b'{"schema": 1, "host_id": "h_AAAAAAAAAAAAAAAAAAAAAAAAAA"}',
            "short id": b'{"schema": 1, "host_id": "h_aaaa"}',
            "empty": b"",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assert_refused(data)

    def test_a_valid_foreign_file_is_read_not_rewritten(self) -> None:
        data = b'{"schema": 1, "host_id": "h_abcdefghijklmnopqrstuvwxyz", "extra": 1}'
        self.write(data)
        self.assertEqual(host_mod.host_id(), "h_abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(self.path.read_bytes(), data)

    def test_the_loser_of_the_exclusive_create_reads_the_winner(self) -> None:
        winner = "h_" + "winner" * 4 + "ab"
        self.assertRegex(winner, HOST_ID)
        self.write(json.dumps({"schema": 1, "host_id": winner}).encode())
        before = self.path.read_bytes()
        real = host_mod._read_bytes  # noqa: SLF001
        reads: list[Path] = []

        def missing_on_the_first_read(path: Path) -> bytes | None:
            # The first read sees no file (the winner had not created it yet),
            # so this process tries the exclusive create and loses.
            reads.append(path)
            return None if len(reads) == 1 else real(path)

        with mock.patch.object(host_mod, "_read_bytes", side_effect=missing_on_the_first_read):
            self.assertEqual(host_mod.host_id(), winner)
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads(self.path.read_text())["host_id"], winner)

    def test_an_empty_file_is_read_again_until_its_creator_writes_it(self) -> None:
        self.write(b"")
        winner = "h_" + "late" * 6 + "zz"
        self.assertRegex(winner, HOST_ID)

        def finish() -> None:
            time.sleep(0.2)
            self.path.write_bytes(json.dumps({"schema": 1, "host_id": winner}).encode())

        writer = threading.Thread(target=finish)
        writer.start()
        try:
            self.assertEqual(host_mod.host_id(), winner)
        finally:
            writer.join()

    def test_info_shape(self) -> None:
        payload = host_mod.info()
        self.assertEqual(set(payload), INFO_KEYS - {"ok"})
        self.assertRegex(payload["host_id"], HOST_ID)
        self.assertEqual(payload["capabilities"], CAPABILITIES)
        self.assertEqual(payload["autonom_version"], __version__)
        with mock.patch.object(host_mod._stdlib_platform, "node", return_value=""):  # noqa: SLF001
            self.assertEqual(host_mod.info()["name"], "localhost")
        with mock.patch.object(host_mod._stdlib_platform, "node", return_value="x" * 200):  # noqa: SLF001
            self.assertEqual(host_mod.info()["name"], "x" * 80)


class HostVerbSurfaceTests(unittest.TestCase):
    """`host info` is classified READONLY and documented in the CLI surface."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("autonom_cli_host", CLI)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        cls.cli = module
        cls.parser = module.build_parser()
        module._ROOT_PARSER = cls.parser  # noqa: SLF001

    def test_readonly_and_never_admitted(self) -> None:
        self.assertIn("host info", self.cli._CONTROL_READONLY)  # noqa: SLF001
        for table in (self.cli._CONTROL_LIFECYCLE, self.cli._CONTROL_HOST,  # noqa: SLF001
                      self.cli._CONTROL_MUTATING, self.cli._CONTROL_FLOW,  # noqa: SLF001
                      self.cli._CONTROL_BY_ACTION):  # noqa: SLF001
            self.assertNotIn("host info", table)
        args = self.parser.parse_args(["host", "info"])
        self.assertEqual(self.cli._control_mode(args), "readonly")  # noqa: SLF001
        self.assertIs(args.func, self.cli.cmd_host_info)

    def test_takes_no_target_flags(self) -> None:
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.parser.parse_args(["host", "info", "--serial", "emulator-5590"])

    def test_documented_in_the_cli_surface(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        block = text.split("## CLI surface", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        lines = [line.split("#", 1)[0].strip() for line in block.splitlines()]
        self.assertIn("autonom host info", lines)


if __name__ == "__main__":
    unittest.main()
