"""The shared fake state file under concurrent writers and readers.

`boot_avd` spawns the fake emulator and polls `fake_adb.py devices` while the
emulator records the device it "booted". The emulator used to rewrite the
state file in place, so a poll that landed between the truncating open and
the write parsed an empty or half-written document: CI on Python 3.11 failed
`BootAvdTests.test_boot_waits_for_boot_completed_and_reports_the_new_serial`
with an AdbError wrapping a JSONDecodeError. These tests pin the two halves
of the fix in `tests/fakes/fake_emulator.py`: a write swaps a complete file
in, and a read waits out a writer that is still rewriting in place.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKE_EMULATOR = ROOT / "tests/fakes/fake_emulator.py"

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _load_fake(path: Path):
    """Import a fake by path: `tests/fakes` is not a package on sys.path."""
    spec = importlib.util.spec_from_file_location(f"_under_test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_emulator = _load_fake(FAKE_EMULATOR)


class FakeStateBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log))

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir()
                      if path not in (self.state, self.log))


class AtomicStateWriteTests(FakeStateBase):
    def test_a_write_swaps_a_new_file_in_instead_of_rewriting_in_place(self) -> None:
        old = {"devices": [["emulator-5554", "device", ""]]}
        new = {"devices": [["emulator-5554", "device", ""],
                           ["emulator-5556", "device", "avd:Pixel_9"]]}
        self.state.write_text(json.dumps(old), encoding="utf-8")
        inode = self.state.stat().st_ino
        # A reader that opened the file just before the write: an in-place
        # rewrite changes the bytes under it, a swap leaves it the old file.
        with open(self.state, encoding="utf-8") as reader:
            fake_emulator.write_state(new)
            self.assertEqual(json.loads(reader.read()), old,
                             "the file was rewritten under an open reader")
        self.assertNotEqual(self.state.stat().st_ino, inode,
                            "rewritten in place: a concurrent reader can see it truncated")
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")), new)
        self.assertEqual(self.leftovers(), [], "the temp file must not be left behind")

    def test_a_strict_reader_never_parses_a_torn_state_file(self) -> None:
        # `fake_adb.py` reads with a bare json.loads, so this is the reader
        # the emulator's writes have to be safe against. A large document
        # widens the window an in-place rewrite leaves open.
        document = {"devices": [[f"emulator-{5554 + 2 * index}", "device",
                                 "product:sdk_gphone64_arm64 model:sdk_gphone64_arm64"]
                                for index in range(4000)]}
        fake_emulator.write_state(document)
        stop = threading.Event()
        writes = [0]

        def keep_writing() -> None:
            while not stop.is_set() and writes[0] < 300:
                fake_emulator.write_state(document)
                writes[0] += 1

        torn: list[str] = []
        reads = 0
        writer = threading.Thread(target=keep_writing, daemon=True)
        writer.start()
        try:
            deadline = time.monotonic() + 30
            while writer.is_alive() and time.monotonic() < deadline:
                try:
                    json.loads(self.state.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    torn.append(str(exc))
                reads += 1
        finally:
            stop.set()
            writer.join(timeout=30)
        self.assertGreater(writes[0], 0)
        self.assertGreater(reads, 0, "no read overlapped the writes")
        self.assertEqual(torn[:3], [], f"{len(torn)} of {reads} reads parsed a torn file "
                                       f"during {writes[0]} writes")
        self.assertEqual(self.leftovers(), [])

    def test_a_booting_fake_emulator_swaps_the_state_file(self) -> None:
        self.state.write_text(json.dumps({"devices": [["emulator-5554", "device", ""]]}),
                              encoding="utf-8")
        inode = self.state.stat().st_ino
        subprocess.run([sys.executable, str(FAKE_EMULATOR), "-avd", "Pixel_9"],
                       check=True, timeout=60, env=dict(os.environ),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["devices"], [["emulator-5554", "device", ""],
                                            ["emulator-5556", "device", "avd:Pixel_9"]])
        self.assertNotEqual(self.state.stat().st_ino, inode,
                            "the boot rewrote the file adb is polling in place")
        self.assertEqual(self.leftovers(), [])


class TolerantStateReadTests(FakeStateBase):
    def test_a_read_waits_out_a_writer_caught_mid_write(self) -> None:
        document = {"avds": ["Pixel_9"], "devices": [["emulator-5554", "device", ""]]}
        text = json.dumps(document)
        # What a reader finds while an in-place writer (a test's plain
        # write_text) is between its truncating open and its last byte.
        self.state.write_text(text[: len(text) // 2], encoding="utf-8")
        finish = threading.Timer(0.05, self.state.write_text, (text,), {"encoding": "utf-8"})
        finish.start()
        try:
            self.assertEqual(fake_emulator.load_state(), document)
        finally:
            finish.cancel()
            finish.join()

    def test_a_document_that_stays_corrupt_is_still_an_error(self) -> None:
        self.state.write_text('{"devices": [', encoding="utf-8")
        with mock.patch.object(fake_emulator, "READ_PATIENCE_SECONDS", 0.2):
            started = time.monotonic()
            with self.assertRaises(json.JSONDecodeError):
                fake_emulator.load_state()
        self.assertGreaterEqual(time.monotonic() - started, 0.2,
                                "gave up before its patience ran out")

    def test_no_state_file_is_an_empty_state(self) -> None:
        self.assertEqual(fake_emulator.load_state(), {})
        self.set_env(AUTONOM_FAKE_STATE=None)
        self.assertEqual(fake_emulator.load_state(), {})
        fake_emulator.write_state({"devices": []})  # nowhere to write: a no-op
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
