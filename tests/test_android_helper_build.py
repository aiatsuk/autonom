"""AH-01 / AH-02 build side: the Android helper is built on first use.

Everything except `RealBuildTests` runs against the fake `javac` and `d8`
(tests/fakes/fake_android_tools.py) in a temporary JDK and SDK layout, so
what is asserted is the argv the tools really received. This Mac has a real
JDK and SDK (and ~/Library/Android/sdk is a discovery root), so every fake
test clears JAVA_HOME, ANDROID_HOME and ANDROID_SDK_ROOT and points HOME at
an empty directory, both in the process environment and in the environment
handed to the module.

`RealBuildTests` compiles the real helper with the real toolchain only when
AUTONOM_REQUIRE_ANDROID_BUILD=1 (then a missing toolchain fails the test);
otherwise it is skipped with the reason.

`SmokeVerdictTests` covers the live smoke's own decisions without a device:
its warm-up wait and its exit code 3 for a device that never warmed up.
`LiveScriptTests` does the same for tests/live/android_helper_live.py (the
CLI-level live check): its adb-call log and wrapper, the device-boundary and
forward verdicts, the coordinate round trip, the tool-free environment, the
quiet-device wait, its exit codes, and its refusal of a serial that runs
another AVD (against the fake adb: no device is contacted). With --boot and
a fake emulator it also refuses a serial another emulator takes during the
boot, and its cleanup sends no command to a serial whose AVD the run did
not confirm (or that names another AVD by then), while it still cleans the
AVD it confirmed.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fakes/fake_android_tools.py"
HELPER = ROOT / "native/android/helper"
SMOKE = ROOT / "tests/live/android_helper_smoke.py"
LIVE = ROOT / "tests/live/android_helper_live.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import android_helper_build as build  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

HEAVY_STDLIB = ("ssl", "http.client", "urllib.request", "xml.sax", "zipfile", "statistics")
JAVAC_FLAGS = ["-Xlint:-options", "-encoding", "UTF-8", "--release", "8"]


def real(paths: list[Path]) -> list[str]:
    return [os.path.realpath(path) for path in paths]


def wrapper(path: Path, tool: str) -> Path:
    """An executable `tool` at `path` that runs the fake with that role."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'AUTONOM_FAKE_TOOL_PATH="$0"; export AUTONOM_FAKE_TOOL_PATH\n'
        f'exec "{sys.executable}" -B "{FAKE}" {tool} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeToolchain(EnvSandboxMixin, unittest.TestCase):
    """A fake JDK and SDK under a temporary directory, nothing real reachable."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.log = self.tmp / "tools.jsonl"
        self.path_dir = self.tmp / "path"
        self.path_dir.mkdir()
        self.jdk = self.tmp / "jdk"
        self.javac = wrapper(self.jdk / "bin/javac", "javac")
        self.java = wrapper(self.jdk / "bin/java", "java")
        (self.jdk / "release").write_text('JAVA_VERSION="21.0.0"\n', encoding="utf-8")
        self.sdk = self.tmp / "sdk"
        self.d8_old = wrapper(self.sdk / "build-tools/35.0.0/d8", "d8")
        self.d8 = wrapper(self.sdk / "build-tools/36.0.0/d8", "d8")
        wrapper(self.sdk / "build-tools/36.0.0-rc1/d8", "d8")
        for name in ("android-9", "android-36", "android-37.0"):
            jar = self.sdk / "platforms" / name / "android.jar"
            jar.parent.mkdir(parents=True)
            jar.write_bytes(b"PK fake " + name.encode())
        self.jar = self.sdk / "platforms/android-37.0/android.jar"
        # The process environment must not lead to the real JDK or SDK.
        self.set_env(JAVA_HOME=None, ANDROID_HOME=None, ANDROID_SDK_ROOT=None,
                     HOME=str(self.home), AUTONOM_HOME=str(self.state))
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.path_dir),
            "AUTONOM_HOME": str(self.state),
            "JAVA_HOME": str(self.jdk),
            "ANDROID_HOME": str(self.sdk),
            "AUTONOM_FAKE_TOOLS_LOG": str(self.log),
        }

    def calls(self, tool: str | None = None, *, compile_only: bool = False) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        if tool:
            rows = [row for row in rows if row["tool"] == tool]
        if compile_only:
            rows = [row for row in rows if row["argv"] not in (["-version"], ["--version"])]
        return rows

    def ensure(self, **extra) -> build.Build:
        env = {**self.env, **{key: value for key, value in extra.items() if value is not None}}
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
        return build.ensure(env=env)

    def builds(self) -> Path:
        return self.state / "android-helper" / "builds"


class DiscoveryTests(FakeToolchain):
    def test_version_order_is_numeric_with_releases_above_previews(self) -> None:
        names = ["9", "36", "37.0", "36.0.0-rc1", "36.0.0", "35.0.0", "UpsideDownCake", "10"]
        self.assertEqual(sorted(names, key=build.version_key),
                         ["UpsideDownCake", "9", "10", "35.0.0", "36", "36.0.0-rc1", "36.0.0", "37.0"])

    def test_newest_build_tools_d8_and_newest_platform_jar(self) -> None:
        roots = build.sdk_roots(self.env)
        self.assertEqual(roots, [self.sdk])
        self.assertEqual(build.find_d8(roots), self.d8)
        self.assertEqual(build.find_android_jar(roots), self.jar)

    def test_sdk_roots_in_documented_order_each_once(self) -> None:
        home_sdk = self.home / "Library/Android/sdk"
        linux_sdk = self.home / "Android/Sdk"
        adb_sdk = self.tmp / "adb-sdk"
        root_sdk = self.tmp / "root-sdk"
        for directory in (home_sdk, linux_sdk, adb_sdk / "platform-tools", root_sdk):
            directory.mkdir(parents=True)
        adb = adb_sdk / "platform-tools/adb"
        adb.write_text("#!/bin/sh\n", encoding="utf-8")
        adb.chmod(0o755)
        env = {**self.env, "ANDROID_SDK_ROOT": str(root_sdk)}
        expected = [self.sdk, root_sdk, adb_sdk, home_sdk, linux_sdk]
        self.assertEqual(real(build.sdk_roots(env, adb_path=str(adb))), real(expected))
        # the same SDK named twice is one root
        env["ANDROID_SDK_ROOT"] = str(self.sdk)
        self.assertEqual(real(build.sdk_roots(env, adb_path=str(adb))),
                         real([self.sdk, adb_sdk, home_sdk, linux_sdk]))

    def test_sdk_found_two_levels_above_the_adb_realpath(self) -> None:
        adb = self.sdk / "platform-tools/adb"
        adb.parent.mkdir()
        adb.write_text("#!/bin/sh\n", encoding="utf-8")
        adb.chmod(0o755)
        link = self.path_dir / "adb"
        link.symlink_to(adb)
        env = {key: value for key, value in self.env.items() if key != "ANDROID_HOME"}
        self.assertEqual(real(build.sdk_roots(env)), real([self.sdk]))  # adb found on PATH
        self.assertEqual(real(build.sdk_roots(env, adb_path=str(link))), real([self.sdk]))

    def test_javac_from_java_home_first_then_path(self) -> None:
        other = wrapper(self.path_dir / "javac", "javac")
        self.assertEqual(build.find_javac(self.env), self.javac)
        without = {key: value for key, value in self.env.items() if key != "JAVA_HOME"}
        self.assertEqual(build.find_javac(without), other)
        # a JAVA_HOME without javac falls back to PATH
        empty = self.tmp / "jre"
        (empty / "bin").mkdir(parents=True)
        self.assertEqual(build.find_javac({**self.env, "JAVA_HOME": str(empty)}), other)

    def test_java_bin_is_the_jdk_of_the_javac_realpath(self) -> None:
        link = self.path_dir / "javac"
        link.symlink_to(self.javac)
        self.assertEqual(build.java_bin(link), self.javac.resolve().parent)

    def test_a_d8_on_path_is_never_used(self) -> None:
        wrapper(self.path_dir / "d8", "d8")  # Homebrew-style d8
        shutil.rmtree(self.sdk / "build-tools")
        result = self.ensure()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "d8 not found")
        self.assertEqual(self.calls(), [])


class MissingToolTests(FakeToolchain):
    def test_no_javac(self) -> None:
        result = self.ensure(JAVA_HOME=None)
        self.assertFalse(result.ok)
        self.assertIsNone(result.dex)
        self.assertEqual(result.reason, "javac not found")
        self.assertIn("JAVA_HOME", result.hint)
        self.assertEqual(self.calls(), [], "nothing may run when a tool is missing")
        self.assertEqual(build.dex_or_reason(env={**self.env, "PATH": str(self.path_dir),
                                                  "JAVA_HOME": str(self.tmp / "none")}),
                         (None, "javac not found"))

    def test_no_sdk_at_all(self) -> None:
        result = self.ensure(ANDROID_HOME=None)
        self.assertEqual(result.reason, "d8 not found")
        self.assertIn("build-tools", result.hint)
        self.assertEqual(self.calls(), [])

    def test_no_platform_jar(self) -> None:
        shutil.rmtree(self.sdk / "platforms")
        result = self.ensure()
        self.assertEqual(result.reason, "android.jar not found")
        self.assertIn("platforms;android-", result.hint)
        self.assertEqual(self.calls(), [])

    def test_a_tool_that_does_not_run(self) -> None:
        result = self.ensure(AUTONOM_FAKE_VERSION_FAIL="javac")
        self.assertFalse(result.ok)
        self.assertTrue(result.reason.startswith("javac does not run: "), result.reason)
        self.assertIn("broken runtime", result.reason)
        result = self.ensure(AUTONOM_FAKE_VERSION_FAIL="d8")
        self.assertTrue(result.reason.startswith("d8 does not run: "), result.reason)
        self.assertEqual(self.calls(compile_only=True), [])
        self.assertFalse(self.builds().exists())
        # a failed probe is not remembered: the working tool is asked again
        self.assertTrue(self.ensure().ok)


class BuildTests(FakeToolchain):
    def test_first_use_builds_with_the_documented_commands(self) -> None:
        result = self.ensure()
        self.assertTrue(result.ok, result)
        self.assertTrue(result.built)
        self.assertRegex(result.build_id, r"^[0-9a-f]{16}$")
        final = self.builds() / result.build_id
        self.assertEqual(result.dex, final / "classes.dex")
        self.assertTrue(result.dex.read_bytes().startswith(b"dex\n"))
        self.assertTrue((final / "complete").is_file())
        self.assertEqual(sorted(path.name for path in final.iterdir()),
                         ["build.json", "classes.dex", "complete"])
        self.assertEqual([path.name for path in self.builds().iterdir()], [result.build_id],
                         "no temporary directory may stay behind")

        javac, d8 = self.calls("javac", compile_only=True), self.calls("d8", compile_only=True)
        self.assertEqual(len(javac), 1)
        self.assertEqual(len(d8), 1)
        sources = sorted(str(path) for path in (HELPER / "src").rglob("*.java"))
        self.assertGreaterEqual(len(sources), 5)
        argv = javac[0]["argv"]
        self.assertEqual(argv[:7], [*JAVAC_FLAGS, "-cp", str(self.jar)])
        self.assertEqual(argv[7], "-d")
        self.assertEqual(Path(argv[8]).name, "classes")
        self.assertEqual(Path(argv[8]).parent.parent, self.builds())
        self.assertTrue(Path(argv[8]).parent.name.startswith(f".tmp-{result.build_id}-"))
        self.assertEqual(argv[9:], sources)
        argv = d8[0]["argv"]
        self.assertEqual(d8[0]["path"], str(self.d8), "the newest build-tools d8 runs")
        self.assertEqual(argv[:6], ["--lib", str(self.jar), "--min-api", "26", "--output",
                                    str(Path(javac[0]["argv"][8]).parent / "dex")])
        self.assertTrue(argv[6:] and all(arg.endswith(".class") for arg in argv[6:]))
        self.assertIn(str(Path(javac[0]["argv"][8]) / "dev/autonom/helper/Main.class"), argv[6:])
        # the SDK d8 script runs `java` from PATH: the JDK's bin is first
        jdk_bin = str(self.jdk.resolve() / "bin")
        for row in (*javac, *d8):
            self.assertEqual(row["path0"], jdk_bin)
            self.assertEqual(row["java"], str(self.jdk.resolve() / "bin/java"))

    def test_second_use_reuses_the_build_and_asks_no_tool(self) -> None:
        first = self.ensure()
        count = len(self.calls())
        second = self.ensure()
        self.assertTrue(second.ok)
        self.assertFalse(second.built)
        self.assertEqual((second.build_id, second.dex), (first.build_id, first.dex))
        self.assertEqual(len(self.calls()), count, "versions are cached; nothing runs again")
        self.assertEqual(build.dex_or_reason(env=self.env), (first.dex, None))
        probe = build.probe(env=self.env)
        self.assertEqual((probe["build"], probe["cached"], probe["failed"]),
                         (first.build_id, True, False))

    def test_version_probe_runs_again_only_when_the_tool_file_changes(self) -> None:
        first = self.ensure()
        self.assertEqual(len(self.calls("javac")), 2)  # -version + compile
        stat = self.javac.stat()
        os.utime(self.javac, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        again = self.ensure()
        self.assertEqual([row["argv"] for row in self.calls("javac")][-1], ["-version"])
        self.assertEqual(again.build_id, first.build_id, "same version text, same build")
        self.assertFalse(again.built)

    def test_new_tool_version_gives_a_new_build(self) -> None:
        first = self.ensure()
        stat = self.d8.stat()
        os.utime(self.d8, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        second = self.ensure(AUTONOM_FAKE_D8_VERSION="D8 9.9.9-fake")
        self.assertNotEqual(second.build_id, first.build_id)
        self.assertTrue(second.built)
        self.assertEqual(len(self.calls("d8", compile_only=True)), 2)

    def test_changed_source_or_android_jar_gives_a_new_build(self) -> None:
        source = self.tmp / "helper"
        shutil.copytree(HELPER / "src", source / "src")
        first = build.ensure(env=self.env, source_dir=source)
        self.assertEqual(first.build_id, build.build_id(first.toolchain, source),
                         "the id does not depend on where the sources live")
        main = source / "src/dev/autonom/helper/Main.java"
        main.write_text(main.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        second = build.ensure(env=self.env, source_dir=source)
        self.assertNotEqual(second.build_id, first.build_id)
        self.assertTrue(second.built)
        stat = self.jar.stat()
        os.utime(self.jar, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        third = build.ensure(env=self.env, source_dir=source)
        self.assertNotIn(third.build_id, (first.build_id, second.build_id))
        self.assertEqual(len(self.calls("javac", compile_only=True)), 3)

    def test_no_sources_is_a_reason_not_a_build(self) -> None:
        empty = self.tmp / "nothing"
        (empty / "src").mkdir(parents=True)
        result = build.ensure(env=self.env, source_dir=empty)
        self.assertEqual(result.reason, "helper source not found")
        self.assertEqual(self.calls(compile_only=True), [])

    def test_state_base_rule(self) -> None:
        self.assertEqual(build.helper_root(self.env), self.state / "android-helper")
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.tmp / "xdg")}
        self.assertEqual(build.helper_root(env), self.tmp / "xdg/autonom/android-helper")
        self.assertEqual(build.helper_root({"HOME": str(self.home)}),
                         self.home / ".local/state/autonom/android-helper")


class FailureTests(FakeToolchain):
    def test_failed_build_is_remembered_and_not_retried(self) -> None:
        first = self.ensure(AUTONOM_FAKE_JAVAC_FAIL="1")
        self.assertFalse(first.ok)
        self.assertEqual(first.reason, "helper build failed: javac exited 1")
        marker = self.builds() / f"{first.build_id}.failed"
        self.assertIn(str(marker), first.hint)
        text = marker.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("javac exited 1\n"))
        self.assertIn("fake compile failure", text)
        self.assertFalse((self.builds() / first.build_id).exists())
        self.assertEqual([path.name for path in self.builds().iterdir()], [marker.name])

        second = self.ensure()  # the tool would work now, but nothing changed
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "helper build failed earlier: javac exited 1")
        self.assertEqual(len(self.calls("javac", compile_only=True)), 1)
        self.assertTrue(build.probe(env=self.env)["failed"])

        marker.unlink()  # the hint's way to retry
        third = self.ensure()
        self.assertTrue(third.ok)
        self.assertEqual(third.build_id, first.build_id)

    def test_new_toolchain_after_a_failure_builds_again(self) -> None:
        first = self.ensure(AUTONOM_FAKE_D8_FAIL="1")
        self.assertEqual(first.reason, "helper build failed: d8 exited 1")
        stat = self.javac.stat()
        os.utime(self.javac, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        second = self.ensure(AUTONOM_FAKE_JAVAC_VERSION="javac 22.0.0-fake")
        self.assertTrue(second.ok, second)
        self.assertNotEqual(second.build_id, first.build_id)

    def test_d8_without_output_is_a_failure(self) -> None:
        result = self.ensure(AUTONOM_FAKE_D8_NO_OUTPUT="1")
        self.assertEqual(result.reason, "helper build failed: d8 wrote no classes.dex")
        self.assertTrue((self.builds() / f"{result.build_id}.failed").is_file())
        self.assertFalse((self.builds() / result.build_id).exists())

    def test_an_unfinished_directory_is_rebuilt_not_trusted(self) -> None:
        first = self.ensure()
        final = self.builds() / first.build_id
        (final / "complete").unlink()
        second = self.ensure()
        self.assertTrue(second.ok)
        self.assertTrue(second.built)
        self.assertTrue((final / "complete").is_file())


class LauncherTests(FakeToolchain):
    """A javac with no JDK around its realpath picks a JDK when it runs (the
    macOS /usr/bin/javac, jenv or asdf shims): its own file does not change
    when the JDK behind it does, so its version is never cached."""

    def setUp(self) -> None:
        super().setUp()
        self.launcher_dir = self.tmp / "usr/bin"
        self.launcher = wrapper(self.launcher_dir / "javac", "javac")
        wrapper(self.launcher_dir / "java", "java")
        self.launcher_env = {"JAVA_HOME": None, "PATH": str(self.launcher_dir)}

    def java_home_tool(self, body: str) -> Path:
        """A stand-in for /usr/libexec/java_home that logs each run."""
        path = self.tmp / "usr/libexec/java_home"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\necho run >> '{self.tmp / 'java_home.log'}'\n{body}\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def java_home_runs(self) -> int:
        log = self.tmp / "java_home.log"
        return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0

    def macos(self, java_home: Path) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(build, "IS_MACOS", True))
        stack.enter_context(mock.patch.object(build, "MACOS_JAVAC_LAUNCHER", self.launcher))
        stack.enter_context(mock.patch.object(build, "MACOS_JAVA_HOME", java_home))
        return stack

    def test_jdk_home_tells_a_jdk_from_a_launcher(self) -> None:
        self.assertEqual(build.jdk_home(self.javac), self.jdk.resolve())
        self.assertIsNone(build.jdk_home(self.launcher))
        modules_only = self.tmp / "jdk-modules"
        javac = wrapper(modules_only / "bin/javac", "javac")
        (modules_only / "lib").mkdir()
        (modules_only / "lib/modules").write_bytes(b"jimage")
        self.assertEqual(build.jdk_home(javac), modules_only.resolve())

    def test_a_new_jdk_behind_a_launcher_builds_again(self) -> None:
        with mock.patch.object(build, "IS_MACOS", False):
            first = self.ensure(**self.launcher_env, AUTONOM_FAKE_JAVAC_FAIL="1",
                                AUTONOM_FAKE_JAVAC_VERSION="javac 25.0.1")
            self.assertEqual(first.reason, "helper build failed: javac exited 1")
            self.assertEqual(first.toolchain.javac, self.launcher)
            # another JDK behind the same launcher file
            second = self.ensure(**self.launcher_env, AUTONOM_FAKE_JAVAC_VERSION="javac 21.0.5")
            self.assertTrue(second.ok, second)
            self.assertTrue(second.built)
            self.assertNotEqual(second.build_id, first.build_id)
            self.assertEqual(second.toolchain.javac_version, "javac 21.0.5")
            third = self.ensure(**self.launcher_env, AUTONOM_FAKE_JAVAC_VERSION="javac 21.0.5")
            self.assertEqual((third.build_id, third.built), (second.build_id, False))
        versions = [row for row in self.calls("javac") if row["argv"] == ["-version"]]
        self.assertEqual(len(versions), 3, "a launcher's version is asked on every call")
        self.assertTrue(all(row["path"] == str(self.launcher) for row in versions))
        cached = build._read_json(self.state / "android-helper/tool-versions.json")
        self.assertNotIn("javac 25.0.1", cached.values())
        self.assertNotIn("javac 21.0.5", cached.values())
        self.assertIn("D8 8.0.0-fake", cached.values(), "d8 is still cached")

    def test_a_jdk_javac_is_cached_even_when_the_jdk_changes_nothing_else(self) -> None:
        self.ensure()
        self.ensure(AUTONOM_FAKE_JAVAC_VERSION="javac 99-not-asked")
        versions = [row for row in self.calls("javac") if row["argv"] == ["-version"]]
        self.assertEqual(len(versions), 1)

    def test_the_macos_launcher_is_looked_through_with_java_home(self) -> None:
        tool = self.java_home_tool(f"echo '{self.jdk}'")
        env = {**self.env, "PATH": str(self.launcher_dir)}
        env.pop("JAVA_HOME")
        with self.macos(tool):
            self.assertEqual(build.find_javac(env), self.jdk / "bin/javac")
            first = build.ensure(env=env)
            self.assertTrue(first.ok, first)
            self.assertEqual(first.toolchain.javac, self.jdk / "bin/javac")
            self.assertEqual(first.toolchain.java_bin, self.jdk.resolve() / "bin")
            count = len(self.calls())
            second = build.ensure(env=env)
        self.assertEqual((second.build_id, second.built), (first.build_id, False))
        self.assertEqual(len(self.calls()), count, "the JDK's javac version is cached")
        self.assertTrue(all(row["path"] != str(self.launcher) for row in self.calls()))
        self.assertEqual(self.java_home_runs(), 3)  # find_javac + two ensure calls

    def test_the_macos_launcher_stays_when_java_home_names_no_jdk(self) -> None:
        env = {**self.env, "PATH": str(self.launcher_dir)}
        env.pop("JAVA_HOME")
        failing = self.java_home_tool("echo 'Unable to locate a Java Runtime.' >&2; exit 1")
        with self.macos(failing):
            self.assertEqual(build.find_javac(env), self.launcher)
        not_a_jdk = self.tmp / "not-a-jdk"
        wrapper(not_a_jdk / "bin/javac", "javac")
        with self.macos(self.java_home_tool(f"echo '{not_a_jdk}'")):
            self.assertEqual(build.find_javac(env), self.launcher)
        self.assertEqual(self.java_home_runs(), 2)

    def test_java_home_is_not_asked_when_it_does_not_apply(self) -> None:
        tool = self.java_home_tool(f"echo '{self.jdk}'")
        env = {**self.env, "PATH": str(self.launcher_dir)}
        # JAVA_HOME set but without javac: the launcher itself would use it
        jre = self.tmp / "jre"
        (jre / "bin").mkdir(parents=True)
        with self.macos(tool):
            self.assertEqual(build.find_javac({**env, "JAVA_HOME": str(jre)}), self.launcher)
        no_java_home = {key: value for key, value in env.items() if key != "JAVA_HOME"}
        with self.macos(tool), mock.patch.object(build, "IS_MACOS", False):
            self.assertEqual(build.find_javac(no_java_home), self.launcher)
        other = wrapper(self.tmp / "shims/javac", "javac")  # a launcher, not the macOS one
        with self.macos(tool):
            self.assertEqual(build.find_javac({**no_java_home, "PATH": str(other.parent)}), other)
        self.assertEqual(self.java_home_runs(), 0)


class StateDirectoryTests(FakeToolchain):
    """A state directory that cannot be written is a reason, never an exception."""

    def setUp(self) -> None:
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root writes into read-only directories")

    def read_only(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o555)
        self.addCleanup(path.chmod, 0o755)
        return path

    def test_unwritable_state_base_gives_a_reason_and_a_hint(self) -> None:
        state = self.read_only(self.tmp / "ro") / "state"
        result = self.ensure(AUTONOM_HOME=str(state))
        self.assertFalse(result.ok)
        self.assertTrue(result.reason.startswith("helper build failed: "), result.reason)
        self.assertIn("Permission denied", result.reason)
        self.assertRegex(result.build_id, r"^[0-9a-f]{16}$")
        self.assertIn(str(state / "android-helper"), result.hint)
        self.assertIn("can be created and written", result.hint)
        self.assertIn("uiautomator", result.hint)
        self.assertEqual(self.calls(compile_only=True), [])
        dex, reason = build.dex_or_reason(env={**self.env, "AUTONOM_HOME": str(state)})
        self.assertIsNone(dex)
        self.assertEqual(reason, result.reason)
        self.assertFalse(state.exists())

    def test_unwritable_builds_directory_gives_a_reason_and_no_marker(self) -> None:
        self.read_only(self.builds())
        result = self.ensure()
        self.assertFalse(result.ok)
        self.assertTrue(result.reason.startswith("helper build failed: "), result.reason)
        self.assertIn(str(self.builds()), result.hint)
        self.assertEqual(list(self.builds().iterdir()), [])
        self.assertEqual(self.calls(compile_only=True), [])

    def test_an_unreadable_source_gives_a_reason(self) -> None:
        source = self.tmp / "helper"
        shutil.copytree(HELPER / "src", source / "src")
        main = source / "src/dev/autonom/helper/Main.java"
        main.chmod(0o000)
        self.addCleanup(main.chmod, 0o644)
        result = build.ensure(env=self.env, source_dir=source)
        self.assertFalse(result.ok)
        self.assertTrue(result.reason.startswith("helper build failed: "), result.reason)
        self.assertIn("Main.java", result.reason)
        self.assertIn("can be read", result.hint)


class ConcurrencyTests(FakeToolchain):
    SCRIPT = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from autonom_lib import android_helper_build as b\n"
        "r = b.ensure(env=json.loads(sys.argv[2]))\n"
        "print(json.dumps({'dex': str(r.dex) if r.dex else None, 'id': r.build_id,"
        " 'built': r.built, 'reason': r.reason}))\n"
    )

    def spawn(self, env: dict) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-B", "-c", self.SCRIPT, str(ROOT / "scripts"), json.dumps(env)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)

    def test_two_processes_at_once_build_once(self) -> None:
        build.find_toolchain(self.env)  # versions cached: both start on the lock
        env = {**self.env, "AUTONOM_FAKE_TOOL_DELAY_S": "1.0"}
        first, second = self.spawn(env), self.spawn(env)
        results = []
        for process in (first, second):
            out, err = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(len(self.calls("javac", compile_only=True)), 1)
        self.assertEqual(len(self.calls("d8", compile_only=True)), 1)
        self.assertEqual(results[0]["dex"], results[1]["dex"])
        self.assertTrue(results[0]["dex"])
        self.assertEqual(sorted(result["built"] for result in results), [False, True])

    def test_a_killed_build_publishes_nothing_and_the_next_one_builds(self) -> None:
        env = {**self.env, "AUTONOM_FAKE_TOOL_DELAY_S": "60"}
        process = self.spawn(env)
        try:
            deadline = time.monotonic() + 60
            while not self.calls("javac", compile_only=True):
                self.assertLess(time.monotonic(), deadline, "javac never started")
                time.sleep(0.05)
        finally:
            os.killpg(process.pid, signal.SIGKILL)  # the build and its javac
            process.communicate(timeout=30)
        names = [path.name for path in self.builds().iterdir()]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith(".tmp-"), names)
        result = self.ensure()
        self.assertTrue(result.ok, result)
        self.assertTrue(result.built)
        self.assertEqual([path.name for path in self.builds().iterdir()], [result.build_id],
                         "the killed build's temporary directory is gone")


class PackagingTests(unittest.TestCase):
    def test_no_binary_in_the_helper_tree_and_gitignore_covers_them(self) -> None:
        binaries = [path for path in HELPER.rglob("*") if path.suffix in (".dex", ".class", ".jar")]
        self.assertEqual(binaries, [])
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.dex", lines)
        self.assertIn("*.class", lines)

    def test_module_level_imports_are_light(self) -> None:
        code = ("import json, sys\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                "import autonom_lib.android_helper_build\n"
                "print(json.dumps(sorted(sys.modules)))\n")
        completed = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                                   text=True, timeout=60, check=True, stdin=subprocess.DEVNULL)
        loaded = set(json.loads(completed.stdout))
        self.assertEqual([name for name in HEAVY_STDLIB if name in loaded], [])
        # the temporary build directory loads its module only when a build runs
        self.assertNotIn("tempfile", loaded)


class RealBuildTests(unittest.TestCase):
    """The real helper with this machine's JDK and SDK (AUTONOM_REQUIRE_ANDROID_BUILD=1)."""

    def test_real_build_produces_a_dex(self) -> None:
        if os.environ.get("AUTONOM_REQUIRE_ANDROID_BUILD") != "1":
            self.skipTest("needs a JDK and the Android SDK; set AUTONOM_REQUIRE_ANDROID_BUILD=1 to run")
        with tempfile.TemporaryDirectory() as state:
            env = {**os.environ, "AUTONOM_HOME": state}
            started = time.monotonic()
            result = build.ensure(env=env)
            self.assertTrue(result.ok, f"{result.reason}: {result.hint}")
            self.assertTrue(result.built)
            self.assertRegex(result.build_id, r"^[0-9a-f]{16}$")
            self.assertEqual(result.dex, Path(state) / "android-helper/builds" / result.build_id
                             / "classes.dex")
            data = result.dex.read_bytes()
            self.assertTrue(data.startswith(b"dex\n"), data[:8])
            self.assertGreater(len(data), 10_000)
            self.assertLess(len(data), 256 * 1024)
            self.assertIn(b"Ldev/autonom/helper/Main;", data)
            seconds = time.monotonic() - started
            again = build.ensure(env=env)
            self.assertEqual((again.build_id, again.dex, again.built),
                             (result.build_id, result.dex, False))
            print(f"\nreal helper build {result.build_id}: {len(data)} bytes in {seconds:.1f} s "
                  f"with {result.toolchain.javac_version!r} and {result.toolchain.d8_version!r}",
                  file=sys.stderr)
        binaries = [path for path in HELPER.rglob("*") if path.suffix in (".dex", ".class", ".jar")]
        self.assertEqual(binaries, [], "the build never writes into the source tree")


def load_smoke() -> types.ModuleType:
    """tests/live/android_helper_smoke.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("android_helper_smoke_under_test", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    """monotonic() and sleep() for the smoke's warm-up loop."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SmokeVerdictTests(unittest.TestCase):
    """The live smoke's own decisions, with no device: the warm-up wait and
    exit code 3 (a device that never warmed up and only timing checks failed)."""

    BUSY = {"events": 37, "last": [{"package": "com.android.systemui", "class": None,
                                    "type": "TYPE_WINDOW_CONTENT_CHANGED", "ms": 1980}],
            "packages": {"com.android.systemui": 37}}

    @classmethod
    def setUpClass(cls) -> None:
        cls.smoke = load_smoke()

    def warm_up(self, replies: list[Any], read_s: float = 2.0,
                budget_left: float = 10_000.0) -> dict[str, Any]:
        """Runs the warm-up against `replies` (the last one repeats), each read
        taking `read_s` on a fake clock."""
        clock = FakeClock()
        queue = list(replies)

        def read() -> dict[str, Any]:
            clock.sleep(read_s)
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(reply, Exception):
                raise reply
            return reply

        fake = types.SimpleNamespace(report={"preflight": {}}, read=read,
                                     budget=types.SimpleNamespace(left=lambda: budget_left))
        with mock.patch.object(self.smoke, "time",
                               types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)):
            self.smoke.Smoke.wait_device_warm(fake)
        return fake.report["preflight"]["device_warm"]

    def test_warm_after_two_quiet_reads_in_a_row(self) -> None:
        busy = {"settled": False, "settle_ms": 2000, "busy": self.BUSY}
        quiet = {"settled": True, "settle_ms": 600}
        slow = {"settled": True, "settle_ms": 1500}  # settled, but not warm yet
        warm = self.warm_up([busy, quiet, slow, quiet, busy, quiet, quiet])
        self.assertTrue(warm["ok"])
        self.assertEqual(warm["attempts"], 7)
        self.assertEqual(warm["last_busy"], self.BUSY)
        self.assertEqual(warm["recent_reads"][-2:], [{"settled": True, "settle_ms": 600}] * 2)
        self.assertNotIn("error", warm)
        self.assertFalse(warm["full_wait"])

    def test_never_warm_gives_up_after_the_limit(self) -> None:
        warm = self.warm_up([{"settled": False, "settle_ms": 2000, "busy": self.BUSY}])
        self.assertFalse(warm["ok"])
        self.assertGreaterEqual(warm["seconds"], self.smoke.WARM_LIMIT_S)
        self.assertLess(warm["seconds"], self.smoke.WARM_LIMIT_S + 5)
        self.assertGreater(warm["attempts"], 50)
        self.assertEqual(warm["last_busy"], self.BUSY)
        self.assertTrue(warm["full_wait"])
        self.assertEqual(warm["limit_s"], self.smoke.WARM_LIMIT_S)

    def test_a_short_budget_is_not_a_cold_device(self) -> None:
        warm = self.warm_up([{"settled": True, "settle_ms": 600}], budget_left=300.0)
        self.assertFalse(warm["ok"])
        self.assertEqual((warm["attempts"], warm["limit_s"], warm["full_wait"]), (1, 0.0, False))
        report = {"cleanup": {"ok": True}, "checks": {"tap_settle": {"ok": False}}}
        self.assertFalse(self.smoke.environment_fault(1, report, ["tap_settle"], [], warm))

    def test_a_failing_read_ends_the_warm_up_without_raising(self) -> None:
        warm = self.warm_up([{"settled": False, "settle_ms": 2000},
                             self.smoke.StepFailed("read failed: {'code': 'internal'}")])
        self.assertFalse(warm["ok"])
        self.assertEqual(warm["attempts"], 1)
        self.assertIn("StepFailed", warm["error"])
        self.assertIsNone(warm["last_busy"], "a reply without busy leaves it unset")
        self.assertFalse(warm["full_wait"])

    def verdict(self, failed: list[str], *, warm_ok: bool | None = False, code: int = 1,
                missing: list[str] | None = None, cleanup_ok: bool = True,
                error: bool = False, full_wait: bool = True,
                raised: tuple[str, ...] = ()) -> bool:
        checks = {name: {"ok": False} for name in failed}
        for name in raised:  # a step that broke off: guarded() adds "error"
            checks[name]["error"] = "StepFailed: type failed: {'code': 'inject_failed'}"
        report: dict[str, Any] = {"cleanup": {"ok": cleanup_ok}, "checks": checks}
        if error:
            report["error"] = "StepFailed: boom"
        warm = None if warm_ok is None else {"ok": warm_ok, "full_wait": full_wait}
        return self.smoke.environment_fault(code, report, failed, missing or [], warm)

    def test_environment_fault_only_for_timing_checks_on_a_cold_device(self) -> None:
        self.assertTrue(self.verdict(["tap_settle"]))
        self.assertTrue(self.verdict(list(self.smoke.TIMING_CHECKS)))
        self.assertFalse(self.verdict(["tap_settle"], warm_ok=True), "a warm device: real failure")
        self.assertFalse(self.verdict(["tap_settle"], warm_ok=None), "no warm-up recorded")
        self.assertFalse(self.verdict(["tap_settle", "duplicate_id"]), "a non-timing check failed")
        self.assertFalse(self.verdict(["read_parity"]))
        self.assertFalse(self.verdict([]), "nothing failed: not a fault")
        self.assertFalse(self.verdict(["tap_settle"], missing=["idle_exit"]))
        self.assertFalse(self.verdict(["tap_settle"], cleanup_ok=False))
        self.assertFalse(self.verdict(["tap_settle"], error=True))
        self.assertFalse(self.verdict(["tap_settle"], code=2), "a refusal keeps its code")
        self.assertFalse(self.verdict(["tap_settle"], full_wait=False), "warm-up cut short")
        self.assertFalse(self.verdict(["tap_settle", "type_unicode"], raised=("type_unicode",)),
                         "an error reply is not a slow device")
        self.assertEqual(set(self.smoke.TIMING_CHECKS),
                         {"tap_settle", "key_back", "swipe_scrolls", "type_unicode", "type_insert",
                          "set_text_unicode"})
        self.assertTrue(set(self.smoke.TIMING_CHECKS) <= set(self.smoke.EXPECTED_CHECKS))

    def run_main(self, checks: dict[str, bool], warm_ok: bool) -> tuple[int, dict, dict]:
        """main() with the device steps replaced: every expected check is
        recorded (ok unless `checks` says otherwise) and the cleanup passes."""
        def fake_run(smoke: Any) -> None:
            smoke.report["preflight"] = {"device_warm": {"ok": warm_ok, "seconds": 240.0,
                                                         "attempts": 96, "last_busy": self.BUSY,
                                                         "full_wait": not warm_ok}}
            for name in self.smoke.EXPECTED_CHECKS:
                smoke.checks[name] = {"ok": checks.get(name, True)}

        def fake_cleanup(smoke: Any) -> None:
            shutil.rmtree(smoke.home, ignore_errors=True)
            smoke.report["cleanup"] = {"ok": True}

        with tempfile.TemporaryDirectory() as evidence, \
                mock.patch.object(self.smoke.Smoke, "run", fake_run), \
                mock.patch.object(self.smoke.Smoke, "cleanup", fake_cleanup):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.smoke.main(["--serial", "emulator-5612", "--avd", "AB_Bench_MCP_API36",
                                        "--adb", "/usr/bin/true", "--evidence-dir", evidence])
            report = json.loads((Path(evidence) / self.smoke.REPORT_NAME).read_text(encoding="utf-8"))
            summary = json.loads(out.getvalue().strip().splitlines()[-1])
        return code, report, summary

    def test_main_exit_codes_and_summary(self) -> None:
        code, report, summary = self.run_main({"tap_settle": False}, warm_ok=False)
        self.assertEqual(code, 3)
        self.assertEqual(report["exit_code"], 3)
        self.assertFalse(report["ok"])
        self.assertIn("environment_fault", report)
        self.assertEqual(summary["failed"], ["tap_settle"])
        self.assertEqual(summary["device_warm"]["ok"], False)
        self.assertEqual(summary["device_warm"]["last_busy"], self.BUSY)
        self.assertEqual(set(summary), {"ok", "report", "failed", "missing", "device_warm"})

        code, report, _ = self.run_main({"tap_settle": False}, warm_ok=True)
        self.assertEqual((code, report["exit_code"]), (1, 1))
        self.assertNotIn("environment_fault", report)

        code, _, _ = self.run_main({"tap_settle": False, "hello": False}, warm_ok=False)
        self.assertEqual(code, 1)

        code, report, summary = self.run_main({}, warm_ok=True)
        self.assertEqual((code, report["ok"], summary["ok"], summary["failed"]), (0, True, True, []))


def load_live() -> types.ModuleType:
    """tests/live/android_helper_live.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("android_helper_live_under_test", LIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveScriptTests(unittest.TestCase):
    """The CLI-level live check's own decisions, with no device."""

    SERIAL = "emulator-5612"

    @classmethod
    def setUpClass(cls) -> None:
        cls.live = load_live()

    def tmp(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    # -- the adb log

    def test_the_wrapper_logs_each_argv_exactly_and_runs_the_real_adb(self) -> None:
        tmp = self.tmp()
        real_adb = tmp / "real adb"
        real_adb.write_text("#!/bin/sh\nprintf 'real:%s\\n' \"$*\"\n", encoding="utf-8")
        real_adb.chmod(0o755)
        log = tmp / "calls.log"
        log.touch()
        wrapper = self.live.write_adb_wrapper(tmp / "adb", str(real_adb), log)
        launch = ["-s", self.SERIAL, "shell",
                  "CLASSPATH=/data/local/tmp/autonom-helper-0123.dex nohup app_process "
                  "/system/bin --nice-name=autonom-helper dev.autonom.helper.Main --build 0123 &"]
        typed = ["-s", self.SERIAL, "shell", "input", "text", "Привет café", ""]
        host = ["forward", "--list"]
        for argv in (launch, host, typed):
            done = subprocess.run([str(wrapper), *argv], capture_output=True, text=True,
                                  timeout=30, check=True)
            self.assertEqual(done.stdout, "real:" + " ".join(argv) + "\n")
        calls = self.live.parse_adb_log(log.read_text(encoding="utf-8"))
        self.assertEqual(calls, [(self.SERIAL, launch[2:]), (None, host), (self.SERIAL, typed[2:])])
        self.assertTrue(self.live.is_helper_launch(calls[0][1]))
        self.assertTrue(self.live.is_helper_setup(calls[0][1]))
        self.assertTrue(self.live.is_adb_input(calls[2][1]))
        self.assertFalse(self.live.is_helper_launch(calls[2][1]))

    def test_call_classes(self) -> None:
        live = self.live
        for size in (["shell", "wm", "size"], ["shell", "dumpsys", "input"],
                     ["shell", "dumpsys window displays"],
                     ["shell", "settings", "get", "system", "user_rotation"]):
            self.assertTrue(live.is_size_call(size), size)
        for other in (["shell", "dumpsys", "activity", "activities"],
                      ["exec-out", "uiautomator", "dump", "/dev/tty"],
                      ["shell", "settings", "get", "global", "airplane_mode_on"]):
            self.assertFalse(live.is_size_call(other), other)
        self.assertTrue(live.is_adb_input(["shell", "input", "tap", "1", "2"]))
        self.assertTrue(live.is_adb_input(["shell", "input keyevent 4"]))
        self.assertTrue(live.is_helper_setup(
            ["push", "/s/classes.dex", "/data/local/tmp/autonom-helper-0123.dex"]))
        self.assertTrue(live.is_helper_setup(["forward", "tcp:0", "localabstract:autonom-helper"]))
        self.assertFalse(live.is_helper_setup(["forward", "--list"]))
        self.assertFalse(live.is_helper_setup(["forward", "tcp:0", "localabstract:autonom-decoy"]))
        self.assertFalse(live.is_helper_setup(["forward", "--remove", "tcp:6100"]))

    def test_the_log_summary_names_every_boundary_breach(self) -> None:
        calls = [(None, ["devices", "-l"]), (None, ["forward", "--list"]),
                 (self.SERIAL, ["shell", "wm", "size"]),
                 (self.SERIAL, ["forward", "--remove", "tcp:6100"]),
                 ("emulator-5580", ["shell", "true"]),
                 (None, ["forward", "--remove-all"])]
        clean = self.live.adb_log_summary(calls[:4], self.SERIAL)
        self.assertEqual((clean["foreign_serials"], clean["unscoped"], clean["remove_all"]),
                         ([], [], 0))
        self.assertEqual(clean["forward_removals"], ["forward --remove tcp:6100"])
        summary = self.live.adb_log_summary(calls, self.SERIAL)
        self.assertEqual(summary["foreign_serials"], ["emulator-5580"])
        self.assertEqual(summary["unscoped"], [["forward", "--remove-all"]])
        self.assertEqual(summary["remove_all"], 1)
        self.assertEqual(summary["calls"], 6)

    # -- forwards

    def test_forwards_verdict(self) -> None:
        verdict = self.live.forwards_verdict
        other = "emulator-5580 tcp:7002 localabstract:scrcpy"
        mine = f"{self.SERIAL} tcp:7100 localabstract:something"
        decoy = f"{self.SERIAL} tcp:7200 localabstract:autonom-decoy"
        helper = f"{self.SERIAL} tcp:7300 localabstract:autonom-helper"
        before = [other, mine]
        ok = verdict(before, [other, mine, decoy], self.SERIAL, decoy=decoy, own_ports={7300},
                     removals=["forward --remove tcp:7300"])
        self.assertEqual(ok, {"missing_this_serial": [], "missing_other_serials": [],
                              "other_serials_removed_by_this_run": [],
                              "own_helper_forwards_left": [], "decoy_listed": True})
        # another session removed its own forward meanwhile: reported, not ours
        gone = verdict(before, [mine, decoy], self.SERIAL, decoy=decoy, own_ports={7300},
                       removals=["forward --remove tcp:7300"])
        self.assertEqual(gone["missing_other_serials"], [other])
        self.assertEqual(gone["other_serials_removed_by_this_run"], [])
        # ... unless an adb call of this run removed that port
        ours = verdict(before, [mine, decoy], self.SERIAL, decoy=decoy, own_ports=set(),
                       removals=["forward --remove tcp:7002"])
        self.assertEqual(ours["other_serials_removed_by_this_run"], [other])
        # this serial's forward missing, Autonom's forward left, the decoy gone
        bad = verdict(before, [other, helper], self.SERIAL, decoy=decoy, own_ports={7300},
                      removals=[])
        self.assertEqual(bad["missing_this_serial"], [mine])
        self.assertEqual(bad["own_helper_forwards_left"], [helper])
        self.assertIs(bad["decoy_listed"], False)
        # a helper forward of this serial that was there before the run is not this run's
        old = verdict([helper], [helper], self.SERIAL, decoy=None, own_ports=set(), removals=[])
        self.assertEqual((old["own_helper_forwards_left"], old["decoy_listed"]), ([], None))

    # -- replies and coordinates

    def test_reply_readers(self) -> None:
        live = self.live
        self.assertEqual(live.last_json('noise\n{"ok": true, "a": 1}\n'), {"ok": True, "a": 1})
        self.assertIsNone(live.last_json("not json\n[1, 2]\n"))
        self.assertEqual(live.outline_lines({"outline": "n1 a\n  n2 b"}), ["n1 a", "  n2 b"])
        self.assertEqual(live.outline_lines({"outline": ["n1 a"]}), ["n1 a"])
        self.assertEqual(live.outline_lines({}), [])
        payload = {"nodes": [{"text": "Search settings", "editable": True, "focused": False},
                             {"text": "abc", "editable": True, "focused": True},
                             {"desc": "Back", "focused": True}],
                   "screen": {"outline": "n1", "signals": {"keyboard": {"visible": True}}}}
        self.assertEqual(live.focused_field(payload)["text"], "abc")
        self.assertEqual(live.labels_of(payload), {"Search settings", "abc", "Back"})
        self.assertEqual(live.signals_of(payload), {"keyboard": {"visible": True}})
        self.assertEqual(live.signals_of({"signals": {"dialog": None}}), {"dialog": None})
        self.assertIsNone(live.signals_of({}))
        self.assertEqual(live.screen_outline(payload), "n1")
        self.assertEqual(live.warning_codes({"warnings": [{"code": "screen_not_settled"}]}),
                         ["screen_not_settled"])

    def test_the_dialog_check_takes_an_alert_or_a_window_with_two_labels(self) -> None:
        live = self.live
        compose = {"kind": "window", "package": "com.android.settings", "title": None,
                   "buttons": [{"label": "Cancel", "resource_id": None},
                               {"label": "OK", "resource_id": None}]}
        self.assertTrue(live.dialog_seen(compose))
        self.assertTrue(live.dialog_seen({"kind": "alert"}))
        for other in ({"kind": "permission"}, {}, None, "alert"):
            self.assertFalse(live.dialog_seen(other), other)
        self.assertEqual(live.button_labels(compose), ["Cancel", "OK"])
        unlabelled = {"buttons": [{"label": None, "resource_id": "android:id/button1"},
                                  {"label": ""}, "x", {"label": "OK"}]}
        self.assertEqual(live.button_labels(unlabelled), ["OK"], "a missing label is no label")
        self.assertEqual(live.button_labels({"buttons": "OK"}), [])
        self.assertEqual(live.button_labels(None), [])

    def test_the_about_labels_are_above_the_fold(self) -> None:
        # the 2026-10-25 read of About emulated device on 1080x2400: 'Build
        # number' below the fold, 'Android version' the clipped last row
        about = {"nodes": [{"text": "Device name", "bounds": [63, 890, 366, 961]},
                           {"text": "Model", "bounds": [63, 1933, 210, 2004]},
                           {"text": "Android version", "bounds": [63, 2345, 432, 2337]}]}
        self.assertEqual(self.live.ABOUT_LABELS, {"Device name", "Model"})
        self.assertTrue(self.live.ABOUT_LABELS <= self.live.labels_of(about))

    def test_settings_restarts_in_a_cleared_task(self) -> None:
        calls: list[tuple[Any, ...]] = []
        adb = types.SimpleNamespace(
            shell=lambda command, **kw: calls.append(("shell", command)),
            run=lambda *args, **kw: calls.append(args))
        self.live.Live.restart_settings(types.SimpleNamespace(adb=adb))
        self.assertEqual(calls, [
            ("shell", "am force-stop com.android.settings"),
            ("shell", "am", "start", "-W", "--activity-clear-task", "-n",
             "com.android.settings/.Settings")])

    def missing_screen_live(self) -> types.SimpleNamespace:
        fake = types.SimpleNamespace(evidence=self.tmp(), current_step=None, checks={})
        fake.save = lambda name, data: self.live.Live.save(fake, name, data)
        fake.screen_missing = lambda what, reply: self.live.Live.screen_missing(fake, what, reply)
        return fake

    def test_a_screen_that_did_not_show_saves_the_last_read_and_names_its_labels(self) -> None:
        live = self.live
        search = {"ok": True, "nodes": [
            {"text": None, "desc": "Navigate up"}, {"text": "Search Settings", "desc": None},
            {"text": "Search Settings", "desc": None}, {"text": "  Recent\n searches ", "desc": ""},
            {"text": "x" * 60}, {"text": "a"}, {"text": "b"}, {"text": "c"}]}
        self.assertEqual(live.first_labels(search),
                         ["Navigate up", "Search Settings", "Recent searches", "x" * 40, "a", "b"])
        self.assertEqual(live.first_labels({"outline": ["n1 page", "  n2 row"]}),
                         ["n1 page", "n2 row"], "a reply with an outline only")
        self.assertEqual(live.first_labels({}), [])
        fake = self.missing_screen_live()
        restarts: list[str] = []
        fake.restart_settings = lambda: restarts.append("restart")
        fake.tree = lambda: live.Reply(["ui", "tree"], 0, search, "", "", 1.0, [])

        def step() -> None:
            live.Live.home_via_cli(fake, 0)

        live.Live.guarded(fake, "s09_agent_screenshot_tap", step)
        self.assertIsNone(fake.current_step, "reset after the step")
        error = fake.checks["s09_agent_screenshot_tap"]["error"]
        self.assertTrue(error.startswith("StepFailed: the Settings list did not show; "
                                         "the last read showed 'Navigate up', 'Search Settings'"),
                        error)
        self.assertIn("saved as s09_agent_screenshot_tap-last-read.json", error)
        saved = fake.evidence / "s09_agent_screenshot_tap-last-read.json"
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), search)
        self.assertEqual(restarts, ["restart"])
        # outside the steps (the read before the quiet wait), and with no read
        failed = live.Live.screen_missing(fake, "the search field did not get focus", None)
        self.assertIsInstance(failed, live.StepFailed)
        self.assertEqual(str(failed), "the search field did not get focus; the last read showed "
                                      "no labels (saved as preflight-last-read.json)")
        self.assertEqual(json.loads((fake.evidence / "preflight-last-read.json")
                                    .read_text(encoding="utf-8")), {})

    def test_a_last_read_that_cannot_be_saved_still_fails_the_step(self) -> None:
        fake = self.missing_screen_live()
        fake.evidence = fake.evidence / "gone"  # no such folder
        fake.current_step = "s06_unicode_type_fill"
        reply = self.live.Reply(["ui", "tree"], 0, {"nodes": [{"text": "Settings"}]}, "", "",
                                1.0, [])
        failed = self.live.Live.screen_missing(fake, "the search field did not get focus", reply)
        self.assertEqual(str(failed), "the search field did not get focus; the last read showed "
                                      "'Settings' (not saved: FileNotFoundError)")

    def test_the_coordinate_note_round_trip_lands_on_the_row(self) -> None:
        live = self.live
        note = "image pixel (x, y) → ui tap --x round(x*2.4) --y round(y*2.4)"
        self.assertEqual(live.note_scale(note), 2.4)
        self.assertEqual(live.note_scale("image pixel (x, y) → ui tap --x round(x*1) --y round(y*1)"), 1.0)
        self.assertIsNone(live.note_scale("no formula"))
        self.assertIsNone(live.note_scale(None))
        row = [0, 682, 1080, 788]
        image, tap = live.image_round_trip(live.center(row), 2.4)
        self.assertEqual(image, (225, 306))
        self.assertEqual(tap, (540, 734))
        self.assertTrue(live.inside(row, tap))
        self.assertFalse(live.inside(row, (540, 788)), "the bottom edge is outside")
        self.assertFalse(live.inside(None, (1, 1)))

    def test_jpeg_size_reads_the_frame_header(self) -> None:
        sof = b"\xff\xc0\x00\x11\x08" + (1000).to_bytes(2, "big") + (450).to_bytes(2, "big")
        data = b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + sof + b"\x00" * 12 + b"\xff\xd9"
        self.assertEqual(self.live.jpeg_size(data), (450, 1000))
        self.assertIsNone(self.live.jpeg_size(b"\x89PNG\r\n"))

    def test_the_dump_helpers_read_escaped_labels(self) -> None:
        xml = ('<?xml version="1.0"?><hierarchy rotation="0"><node index="0" '
               'text="Network &amp; internet" bounds="[0,682][1080,788]"/>'
               '<node index="1" text="" content-desc="Search" bounds="[0,0][10,10]"/></hierarchy>')
        self.assertEqual(self.live.dump_labels(xml), {"Network & internet", "Search"})
        self.assertEqual(self.live.dump_point(xml, "Network & internet"), (540, 735))
        self.assertIsNone(self.live.dump_point(xml, "Apps"))
        self.assertEqual(self.live.dump_labels("<hierarchy"), set(), "a cut dump has no labels")

    # -- the environment of step 13

    def test_the_tool_free_environment_drops_every_jdk(self) -> None:
        tmp = self.tmp()
        jdk_bin, other = tmp / "jdk" / "bin", tmp / "bin"
        for directory in (jdk_bin, other):
            directory.mkdir(parents=True)
        javac = jdk_bin / "javac"
        javac.write_text("#!/bin/sh\n", encoding="utf-8")
        javac.chmod(0o755)
        (other / "javac").write_text("not executable", encoding="utf-8")
        env = {"PATH": os.pathsep.join([str(jdk_bin), str(other), ""]), "JAVA_HOME": str(tmp),
               "ANDROID_HOME": str(tmp), "ANDROID_SDK_ROOT": str(tmp), "HOME": "/home/x"}
        out = self.live.stripped_tools_env(env)
        self.assertEqual(out, {"PATH": str(other), "HOME": "/home/x"})
        self.assertEqual(env["JAVA_HOME"], str(tmp), "the input is not changed")

    # -- the quiet-device wait and the verdict

    def quiet(self, reads: list[Any], read_s: float = 1.0,
              budget_left: float = 10_000.0) -> dict[str, Any]:
        clock = FakeClock()
        queue = list(reads)

        def quiet_read() -> dict[str, Any]:
            clock.sleep(read_s)
            read = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(read, Exception):
                raise read
            return read

        fake = types.SimpleNamespace(report={"preflight": {}}, quiet_read=quiet_read,
                                     budget=types.SimpleNamespace(left=lambda: budget_left))
        with mock.patch.object(self.live, "time",
                               types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)):
            self.live.Live.wait_device_quiet(fake)
        return fake.report["preflight"]["device_quiet"]

    def test_quiet_after_two_settled_reads_in_a_row(self) -> None:
        busy = {"settled": False, "settle_ms": 2000}
        quiet = {"settled": True, "settle_ms": 400}
        slow = {"settled": True, "settle_ms": 1500}
        fallback = {"settled": None, "settle_ms": None, "ui_backend": "uiautomator"}
        result = self.quiet([busy, quiet, slow, quiet, fallback, quiet, quiet])
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 7)
        self.assertFalse(result["full_wait"])

    def test_never_quiet_gives_up_after_the_whole_wait(self) -> None:
        result = self.quiet([{"settled": False, "settle_ms": 2000}])
        self.assertFalse(result["ok"])
        self.assertTrue(result["full_wait"])
        self.assertGreaterEqual(result["seconds"], self.live.QUIET_LIMIT_S)
        self.assertLess(result["seconds"], self.live.QUIET_LIMIT_S + 5)

    def test_a_short_budget_or_a_broken_read_is_not_a_noisy_device(self) -> None:
        short = self.quiet([{"settled": True, "settle_ms": 400}], budget_left=300.0)
        self.assertEqual((short["ok"], short["full_wait"], short["attempts"]), (False, False, 1))
        broken = self.quiet([{"settled": False, "settle_ms": 2000}, OSError("adb went away")])
        self.assertEqual((broken["ok"], broken["full_wait"]), (False, False))
        self.assertIn("OSError", broken["error"])

    def test_environment_fault_only_for_timing_checks_on_a_noisy_device(self) -> None:
        def fault(failed: list[str], *, quiet_ok: bool = False, full_wait: bool = True,
                  cleanup_ok: bool = True, raised: tuple[str, ...] = (), code: int = 1,
                  missing: list[str] | None = None) -> bool:
            checks = {name: {"ok": False} for name in failed}
            for name in raised:
                checks[name]["error"] = "StepFailed: boom"
            checks["s15_cleanup"] = {"ok": cleanup_ok}
            names = failed + ([] if cleanup_ok else ["s15_cleanup"])
            return self.live.environment_fault(code, {"checks": checks}, names, missing or [],
                                               {"ok": quiet_ok, "full_wait": full_wait})
        self.assertTrue(fault(["s05_agent_tap_settled"]))
        self.assertTrue(fault(list(self.live.TIMING_CHECKS)))
        self.assertFalse(fault(["s05_agent_tap_settled"], quiet_ok=True))
        self.assertFalse(fault(["s05_agent_tap_settled"], full_wait=False))
        self.assertFalse(fault(["s05_agent_tap_settled", "s08_dialog_signal"]))
        self.assertFalse(fault(["s05_agent_tap_settled"], cleanup_ok=False))
        self.assertFalse(fault(["s06_unicode_type_fill"], raised=("s06_unicode_type_fill",)))
        self.assertFalse(fault(["s05_agent_tap_settled"], code=2))
        self.assertFalse(fault(["s05_agent_tap_settled"], missing=["s12_idle_stop"]))
        self.assertFalse(fault([]))
        self.assertTrue(set(self.live.TIMING_CHECKS) <= set(self.live.EXPECTED_CHECKS))
        self.assertEqual(len(self.live.EXPECTED_CHECKS), 15)

    def run_main(self, failing: set[str], quiet_ok: bool) -> tuple[int, dict, dict]:
        """main() with the device steps replaced: every check is recorded (ok
        unless named in `failing`)."""
        def fake_run(live: Any) -> None:
            live.report["preflight"] = {"device_quiet": {"ok": quiet_ok, "full_wait": not quiet_ok}}
            for name in self.live.EXPECTED_CHECKS[:-1]:
                live.checks[name] = {"ok": name not in failing}

        def fake_cleanup(live: Any) -> None:
            shutil.rmtree(live.work, ignore_errors=True)
            live.checks["s15_cleanup"] = {"ok": "s15_cleanup" not in failing}

        evidence = self.tmp()
        with mock.patch.object(self.live.Live, "run", fake_run), \
                mock.patch.object(self.live.Live, "cleanup", fake_cleanup):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.live.main(["--serial", self.SERIAL, "--avd", "AB_Bench_MCP_API36",
                                       "--adb", "/usr/bin/true", "--evidence-dir", str(evidence)])
        report = json.loads((evidence / self.live.REPORT_NAME).read_text(encoding="utf-8"))
        summary = json.loads(out.getvalue().strip().splitlines()[-1])
        return code, report, summary

    def test_main_exit_codes(self) -> None:
        code, report, summary = self.run_main(set(), quiet_ok=True)
        self.assertEqual((code, report["ok"], report["exit_code"], summary["failed"]),
                         (0, True, 0, []))
        code, report, _ = self.run_main({"s05_agent_tap_settled"}, quiet_ok=False)
        self.assertEqual((code, report["exit_code"]), (3, 3))
        self.assertIn("environment_fault", report)
        code, report, _ = self.run_main({"s05_agent_tap_settled"}, quiet_ok=True)
        self.assertEqual(code, 1)
        code, _, _ = self.run_main({"s05_agent_tap_settled", "s15_cleanup"}, quiet_ok=False)
        self.assertEqual(code, 1, "a failed cleanup is never an environment fault")

    # -- the refusal, against the fake adb

    def refuse(self, state: dict[str, Any], *extra: str) -> tuple[int, dict, list[list[str]]]:
        tmp = self.tmp()
        state_path, log = tmp / "adb-state.json", tmp / "adb-log.jsonl"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        work = tmp / "work"
        work.mkdir()
        evidence = tmp / "evidence"
        with mock.patch.dict(os.environ, {"AUTONOM_FAKE_STATE": str(state_path),
                                          "AUTONOM_FAKE_LOG": str(log)}), \
                mock.patch.object(self.live.tempfile, "mkdtemp", return_value=str(work)), \
                contextlib.redirect_stdout(io.StringIO()):
            code = self.live.main(["--serial", self.SERIAL, "--avd", "AB_Bench_MCP_API36",
                                   "--adb", str(FAKE_ADB), "--evidence-dir", str(evidence),
                                   *extra])
        self.assertFalse(work.exists(), "the run's temporary directory is removed")
        report = json.loads((evidence / self.live.REPORT_NAME).read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()] if log.exists() else []
        return code, report, [row["argv"] for row in rows]

    def test_a_serial_running_another_avd_is_refused_touching_nothing(self) -> None:
        code, report, calls = self.refuse({
            "devices": [[self.SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "avd_names": {self.SERIAL: "Some_Other_AVD"}}, "--boot")
        self.assertEqual((code, report["exit_code"], report["ok"]), (2, 2, False))
        self.assertIn("Some_Other_AVD", report["refused"])
        self.assertEqual((report["checks"], report["missing_checks"]), ({}, []))
        self.assertEqual(calls, [["devices"], ["-s", self.SERIAL, "emu", "avd", "name"]])

    def test_a_serial_that_is_not_running_is_refused_without_boot(self) -> None:
        code, report, calls = self.refuse({"devices": [["emulator-5580", "device", "product:x"]]})
        self.assertEqual(code, 2)
        self.assertIn("pass --boot", report["refused"])
        self.assertEqual(calls, [["devices"]], "another device is listed, never addressed")

    # -- another emulator on the serial during --boot, against the fake adb

    # Another session's device on the serial: its own helper, its files, and
    # airplane mode on. Nothing of it may change.
    FOREIGN = {"helper_procs": {"7001": "autonom-helper\0dev.autonom.helper.Main\0"},
               "device_files": ["autonom-helper-theirs.dex", "autonom-helper.log", "other.txt"],
               "settings": {"airplane_mode_on": "1"},
               "forwards": ["emulator-5612 tcp:7002 localabstract:autonom-helper"]}

    FAKE_EMULATOR = (
        "import json, os, sys, tempfile, time\n"
        "with open(os.environ['LIVE_FAKE_EMULATOR'], encoding='utf-8') as handle:\n"
        "    behaviour = json.load(handle)\n"
        "path = os.environ['AUTONOM_FAKE_STATE']\n"
        "serial = 'emulator-' + sys.argv[sys.argv.index('-port') + 1]\n"
        "if behaviour.get('holder'):\n"
        "    with open(path, encoding='utf-8') as handle:\n"
        "        state = json.load(handle)\n"
        "    state.setdefault('devices', []).append(\n"
        "        [serial, behaviour['holder'], 'product:sdk_gphone64_arm64'])\n"
        "    state.setdefault('avd_names', {})[serial] = behaviour['avd']\n"
        "    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path))\n"
        "    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:\n"
        "        json.dump(state, handle)\n"
        "    os.replace(temporary, path)\n"
        "if 'exit' in behaviour:\n"
        "    sys.exit(behaviour['exit'])\n"
        "time.sleep(120)\n")

    def boot_run(self, behaviour: dict[str, Any]
                 ) -> tuple[int, dict, list[list[str]], dict[str, Any]]:
        """main() with --boot and a fake emulator that does `behaviour` on
        the serial; the code, the report, the fake adb's calls and its final
        state."""
        tmp = self.tmp()
        state_path, log = tmp / "adb-state.json", tmp / "adb-log.jsonl"
        state_path.write_text(json.dumps({
            "devices": [["emulator-5580", "device", "product:x"]], **self.FOREIGN}),
            encoding="utf-8")
        emulator, behaviour_path = tmp / "emulator", tmp / "behaviour.json"
        emulator.write_text(f"#!{sys.executable}\n" + self.FAKE_EMULATOR, encoding="utf-8")
        emulator.chmod(0o755)
        behaviour_path.write_text(json.dumps(behaviour), encoding="utf-8")
        work, evidence = tmp / "work", tmp / "evidence"
        work.mkdir()
        with mock.patch.dict(os.environ, {"AUTONOM_FAKE_STATE": str(state_path),
                                          "AUTONOM_FAKE_LOG": str(log),
                                          "LIVE_FAKE_EMULATOR": str(behaviour_path)}), \
                mock.patch.object(self.live.tempfile, "mkdtemp", return_value=str(work)), \
                mock.patch.object(self.live, "host_load", return_value=1.0), \
                mock.patch.object(self.live, "HOLDER_WAIT_S", 0.3), \
                contextlib.redirect_stdout(io.StringIO()):
            code = self.live.main(["--serial", self.SERIAL, "--avd", "AB_Bench_MCP_API36",
                                   "--adb", str(FAKE_ADB), "--evidence-dir", str(evidence),
                                   "--boot", "--emulator", str(emulator),
                                   "--boot-timeout", "60"])
        self.assertFalse(work.exists(), "the run's temporary directory is removed")
        report = json.loads((evidence / self.live.REPORT_NAME).read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()] if log.exists() else []
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return code, report, [row["argv"] for row in rows], state

    def assert_foreign_untouched(self, calls: list[list[str]], state: dict[str, Any]) -> None:
        identity = ["-s", self.SERIAL, "emu", "avd", "name"]
        self.assertEqual([call for call in calls if call not in (["devices"], identity)], [],
                         "only `adb devices` and the AVD name were read")
        for key, value in self.FOREIGN.items():
            self.assertEqual(state.get(key), value, key)

    def assert_child_gone(self, report: dict[str, Any]) -> None:
        with self.assertRaises(ProcessLookupError):
            os.kill(report["boot"]["pid"], 0)

    def test_boot_refuses_a_serial_another_emulator_takes_and_sends_it_nothing(self) -> None:
        foreign = "Someone_Elses_AVD"
        for behaviour, child_runs in (
                ({"holder": "device", "avd": foreign}, True),   # it comes up while ours runs
                ({"holder": "offline", "avd": foreign, "exit": 1}, False),  # ours exits
                ({"holder": "device", "avd": foreign, "exit": 1}, False)):  # either path
            with self.subTest(behaviour=behaviour):
                code, report, calls, state = self.boot_run(behaviour)
                self.assertEqual((code, report["exit_code"], report["ok"]), (2, 2, False))
                self.assertIn(foreign, report["refused"])
                self.assertNotIn("error", report)
                self.assertNotIn("cleanup", report, "a refusal runs no device cleanup")
                self.assertEqual((report["checks"], report["missing_checks"]), ({}, []))
                argv = report["boot"]["argv"]
                self.assertIn("-no-snapshot", argv)
                self.assertEqual(argv[argv.index("-port") + 1], "5612")
                self.assert_foreign_untouched(calls, state)
                self.assertIn(["-s", self.SERIAL, "emu", "avd", "name"], calls)
                self.assert_child_gone(report)
                if child_runs:
                    self.assertIn("came up as AVD", report["refused"])
                    self.assertIs(report["boot"]["child_stopped"], True)
                elif behaviour["holder"] == "offline":
                    self.assertIn("exited", report["refused"])
                    self.assertEqual(report["boot"]["serial_held_by"], foreign)

    def test_an_emulator_that_exits_before_any_avd_is_named_gets_no_device_cleanup(self) -> None:
        code, report, calls, state = self.boot_run({"exit": 1})
        self.assertEqual((code, report["exit_code"]), (1, 1))
        self.assertIn("the emulator exited with 1", report["error"])
        cleanup = report["cleanup"]
        self.assertEqual((cleanup["device_identity"], cleanup["device_steps_skipped"]),
                         ("never verified", True))
        for key in ("killed_helper_pids", "device_files_removed", "airplane_mode_off",
                    "forwards", "session_stopped"):
            self.assertNotIn(key, cleanup)
        self.assertIs(cleanup["emulator_stopped"], True)
        self.assertIs(report["checks"]["s15_cleanup"]["ok"], True)
        self.assertIn(["devices"], calls)
        self.assertEqual([call for call in calls if call != ["devices"]], [],
                         "no command named the serial")
        self.assert_foreign_untouched(calls, state)
        self.assert_child_gone(report)

    # -- the cleanup's own identity check, against the fake adb

    def cleanup_run(self, state: dict[str, Any], *, verified: bool = True,
                    forwards_before: list[str] | None = None,
                    **attributes: Any) -> tuple[Any, list[list[str]], dict[str, Any]]:
        tmp = self.tmp()
        state_path, log = tmp / "adb-state.json", tmp / "adb-log.jsonl"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        evidence = tmp / "evidence"
        evidence.mkdir()
        args = self.live.parse_args(["--serial", self.SERIAL, "--avd", "AB_Bench_MCP_API36",
                                     "--adb", str(FAKE_ADB), "--evidence-dir", str(evidence)])
        report = {"checks": {}, "timings": {}}
        with mock.patch.dict(os.environ, {"AUTONOM_FAKE_STATE": str(state_path),
                                          "AUTONOM_FAKE_LOG": str(log)}):
            live = self.live.Live(args, report)
            live.target_verified = verified
            live.forwards_before = list(forwards_before or [])
            for name, value in attributes.items():
                setattr(live, name, value)
            live.cleanup()
        self.assertFalse(live.work.exists())
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()] if log.exists() else []
        return live, [row["argv"] for row in rows], json.loads(state_path.read_text("utf-8"))

    def test_the_cleanup_sends_nothing_once_the_serial_names_another_avd(self) -> None:
        state = {"devices": [[self.SERIAL, "device", "product:x"]],
                 "avd_names": {self.SERIAL: "Someone_Elses_AVD"}, **self.FOREIGN}
        live, calls, after = self.cleanup_run(state, session_live=True, decoy_port=7200)
        cleanup = live.report["cleanup"]
        self.assertEqual((cleanup["device_identity"], cleanup["avd_name_now"]),
                         ("changed", "Someone_Elses_AVD"))
        self.assertIs(cleanup["device_steps_skipped"], True)
        self.assertIs(live.checks["s15_cleanup"]["ok"], False, "this run's cleanup is undone")
        self.assertEqual(calls, [["-s", self.SERIAL, "emu", "avd", "name"]])
        self.assertEqual(live.cli.history, [], "no `session stop` through the CLI")
        self.assert_foreign_untouched(calls, after)

    def test_the_cleanup_still_cleans_the_avd_it_confirmed(self) -> None:
        mine = f"{self.SERIAL} tcp:7100 localabstract:something"
        state = {"devices": [[self.SERIAL, "device", "product:x"]],
                 "avd_names": {self.SERIAL: "AB_Bench_MCP_API36"},
                 "helper_procs": {"4242": "autonom-helper\0dev.autonom.helper.Main\0"},
                 "device_files": ["autonom-helper-abc.dex", "autonom-helper.log", "other.txt"],
                 "forwards": [mine],
                 # the script sends the settings read as one shell string, which
                 # the fake's settings table does not parse: a canned answer
                 "fail": {f"-s {self.SERIAL} shell settings get global airplane_mode_on":
                          [0, "0"]}}
        live, calls, after = self.cleanup_run(state, forwards_before=[mine])
        cleanup = live.report["cleanup"]
        self.assertEqual(cleanup["device_identity"], "verified")
        self.assertNotIn("device_steps_skipped", cleanup)
        self.assertEqual(cleanup["killed_helper_pids"], [4242])
        self.assertEqual(cleanup["device_files_removed"],
                         ["autonom-helper-abc.dex", "autonom-helper.log"])
        self.assertEqual((after["helper_procs"], after["device_files"], after["forwards"]),
                         ({}, ["other.txt"], [mine]))
        self.assertIs(live.checks["s15_cleanup"]["ok"], True, cleanup)
        self.assertEqual({call[1] for call in calls if call[:1] == ["-s"]}, {self.SERIAL})
        self.assertNotIn("--remove-all", [word for call in calls for word in call])

    def test_never_verified_cleanup_reads_nothing_from_the_serial(self) -> None:
        live, calls, after = self.cleanup_run(
            {"devices": [[self.SERIAL, "device", "product:x"]],
             "avd_names": {self.SERIAL: "Someone_Elses_AVD"}, **self.FOREIGN}, verified=False)
        self.assertEqual(live.report["cleanup"]["device_identity"], "never verified")
        self.assertIs(live.checks["s15_cleanup"]["ok"], True)
        self.assertEqual(calls, [])
        self.assert_foreign_untouched(calls, after)

    def test_usage_errors(self) -> None:
        for argv in (["--serial", "R58M123", "--avd", "A", "--evidence-dir", "x"],
                     ["--serial", self.SERIAL, "--avd", "a b", "--evidence-dir", "x"],
                     ["--serial", self.SERIAL, "--avd", "A", "--evidence-dir", "x",
                      "--alert-app", "rm -rf"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                self.live.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
