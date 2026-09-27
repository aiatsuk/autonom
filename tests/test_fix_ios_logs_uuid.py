"""iOS log records of a reinstalled app, matched by binary UUID — and the
process checks that took a writer under a non-ASCII home for a stranger.

Found live on WoolBox (iOS 26.5 simulator, Xcode 27), after the fix round:

1. After the same build was uninstalled and installed again (`session clear
   --strategy reinstall`, or by hand), the unified log kept naming the
   FIRST container the binary's UUID had been seen at: the app ran from
   `.../441D5220-.../Runner-26.09.23.app/Runner` while all 817 of its records
   said `.../FE85D28A-.../Runner-26.09.23.app/Runner`, a deleted directory.
   The predicate `processImagePath BEGINSWITH "<current bundle>/"` matched
   none of them: `--log-stream` wrote 0 bytes and `logs tail --package`
   returned 0 lines while the app ran. Every record carries the binary's
   Mach-O UUID (`processImageUUID`, `senderImageUUID`), which `log`
   predicates cannot compare, so the server predicate now names the
   executable and the client side keeps the records whose image UUID is the
   installed binary's LC_UUID.
2. `processes.command_of` ran `ps` under the caller's locale: with
   `LC_ALL=C` macOS prints `caf\\u00e9` as `cafM-CM-)`, so the log writer of a
   session under a non-ASCII AUTONOM_HOME read as `pid_reused` and was left
   running, and `stop_log_writer` dropped its registry row, so `cleanup
   --all` lost it too.
3. A registry row written by the round-1 tree records the plain stream path
   as its signature, which `tail -f <stream>` carries as well.

Hermetic: synthetic Mach-O bytes, the fakes, a temp AUTONOM_HOME. The only
host binary read is /usr/bin/true, and those checks are skipped where it is
not Mach-O (or `dwarfdump` cannot run).
"""
from __future__ import annotations

import json
import os
import plistlib
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
BUNDLE = "com.example.knit"
OTHER_BUNDLE = "com.example.otherflutter"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import ios_simctl, logs, processes, session  # noqa: E402
from autonom_lib.platform import IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

# The app's arm64 and x86_64 slices, another Flutter app's binary, the
# Flutter engine inside the app, and a later build of the app.
APP_ARM64 = "01EFEDDA-6237-3761-BFA5-4D5F6FCEF5B7"
APP_X86_64 = "928C792A-4E86-3C1F-B293-710B1371E6CD"
OTHER_APP = "5B1D8E0C-2F43-3A77-9C1E-7D2A64B0F3A9"
FLUTTER_ENGINE = "3C9A1F20-8D41-3B6E-A0F2-19C7E5D48B11"
NEXT_BUILD = "A0B1C2D3-E4F5-3061-8273-94A5B6C7D8E9"
CANONICAL = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")

CAFE = "café"

# --- synthetic Mach-O ---------------------------------------------------------------

LC_SEGMENT = 0x1
LC_SEGMENT_64 = 0x19
LC_UUID = 0x1B
CPU_X86_64 = 0x01000007
CPU_ARM64 = 0x0100000C
CPU_PPC = 0x12
MH_EXECUTE = 2


def thin(uuid: str | None, *, bits: int = 64, order: str = "<", cputype: int = CPU_ARM64,
         payload: int = 0, sizeofcmds: int | None = None, ncmds: int | None = None) -> bytes:
    """A minimal thin image: a segment command, LC_UUID (unless None), another
    segment command, then `payload` bytes standing in for the code."""
    segment = (struct.pack(order + "II", LC_SEGMENT_64, 72) + bytes(64) if bits == 64
               else struct.pack(order + "II", LC_SEGMENT, 56) + bytes(48))
    commands = [segment]
    if uuid is not None:
        commands.append(struct.pack(order + "II", LC_UUID, 24)
                        + bytes.fromhex(uuid.replace("-", "")))
    commands.append(segment)
    body = b"".join(commands)
    magic = 0xFEEDFACF if bits == 64 else 0xFEEDFACE
    header = struct.pack(order + "IIIIIII", magic, cputype, 0, MH_EXECUTE,
                         len(commands) if ncmds is None else ncmds,
                         len(body) if sizeofcmds is None else sizeofcmds, 0)
    if bits == 64:
        header += struct.pack(order + "I", 0)
    return header + body + bytes(payload)


def fat(slices: list[tuple[int, bytes]], *, wide: bool = False) -> bytes:
    """A universal binary (fat, or fat64 with ``wide``) of ``(cputype,
    image)`` slices, big-endian as on disk, each aligned to 16 bytes."""
    entry = 32 if wide else 20
    start = (8 + entry * len(slices) + 15) // 16 * 16
    table = b""
    body = b""
    for cputype, image in slices:
        offset = start + len(body)
        if wide:
            table += struct.pack(">IIQQII", cputype, 0, offset, len(image), 4, 0)
        else:
            table += struct.pack(">IIIII", cputype, 0, offset, len(image), 4)
        body += image + bytes(-len(image) % 16)
    head = struct.pack(">II", 0xCAFEBABF if wide else 0xCAFEBABE, len(slices)) + table
    return head + bytes(start - len(head)) + body


def app_binary() -> bytes:
    """The app as a simulator build ships it: x86_64 and arm64 slices."""
    return fat([(CPU_X86_64, thin(APP_X86_64, cputype=CPU_X86_64)),
                (CPU_ARM64, thin(APP_ARM64))])


def info_plist(bundle: Path, identifier: str, executable: str = "Runner") -> None:
    """The Info.plist every installed app bundle carries."""
    (bundle / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": identifier, "CFBundleExecutable": executable}))


def record_line(image: str, message: str, *, uuid: str | None, sender: str | None = None,
                sender_uuid: str | None = None, subsystem: str = "") -> str:
    """One `log --style ndjson` record, trimmed to the keys that matter."""
    record: dict[str, Any] = {
        "timestamp": "2026-09-26 20:01:19.492171+0200", "messageType": "Default",
        "processImagePath": image, "senderImagePath": sender or image,
        "subsystem": subsystem, "eventMessage": message,
    }
    if uuid is not None:
        record["processImageUUID"] = uuid
    if sender_uuid is not None or uuid is not None:
        record["senderImageUUID"] = sender_uuid or uuid
    return json.dumps(record)


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _gone(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


class _Tmp(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        ios_simctl.reset_caches()
        self.addCleanup(ios_simctl.reset_caches)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


# --- 1. the Mach-O reader -----------------------------------------------------------


class MachOReaderTests(_Tmp):
    def test_thin_arm64(self) -> None:
        self.assertEqual(ios_simctl.binary_uuids(self.write("arm64", thin(APP_ARM64))),
                         {APP_ARM64})

    def test_thin_x86_64(self) -> None:
        path = self.write("x86_64", thin(APP_X86_64, cputype=CPU_X86_64))
        self.assertEqual(ios_simctl.binary_uuids(path), {APP_X86_64})

    def test_both_byte_orders_and_widths(self) -> None:
        for bits in (32, 64):
            for order in ("<", ">"):
                with self.subTest(bits=bits, order=order):
                    path = self.write(f"thin{bits}{order == '<'}",
                                      thin(APP_ARM64, bits=bits, order=order, cputype=CPU_PPC))
                    self.assertEqual(ios_simctl.binary_uuids(path), {APP_ARM64})

    def test_fat_with_two_slices(self) -> None:
        self.assertEqual(ios_simctl.binary_uuids(self.write("fat", app_binary())),
                         {APP_X86_64, APP_ARM64})

    def test_fat64_with_two_slices(self) -> None:
        data = fat([(CPU_X86_64, thin(APP_X86_64, cputype=CPU_X86_64)),
                    (CPU_ARM64, thin(APP_ARM64))], wide=True)
        self.assertEqual(ios_simctl.binary_uuids(self.write("fat64", data)),
                         {APP_X86_64, APP_ARM64})

    def test_the_form_is_canonical_upper_case(self) -> None:
        path = self.write("lower", thin(APP_ARM64.lower()))
        (found,) = ios_simctl.binary_uuids(path)
        self.assertRegex(found, CANONICAL)
        self.assertEqual(found, APP_ARM64)

    def test_a_slice_without_lc_uuid_contributes_nothing(self) -> None:
        self.assertEqual(ios_simctl.binary_uuids(self.write("none", thin(None))), set())
        data = fat([(CPU_X86_64, thin(None, cputype=CPU_X86_64)), (CPU_ARM64, thin(APP_ARM64))])
        self.assertEqual(ios_simctl.binary_uuids(self.write("half", data)), {APP_ARM64})
        zero = thin("00000000-0000-0000-0000-000000000000")
        self.assertEqual(ios_simctl.binary_uuids(self.write("zero", zero)), set(),
                         "an all-zero UUID identifies nothing")

    def test_truncated_and_garbage_files_are_the_empty_set(self) -> None:
        junk = random.Random(7)
        cases = {
            "empty": b"",
            "magic only": b"\xcf\xfa\xed\xfe",
            "cut header": thin(APP_ARM64)[:20],
            "cut commands": thin(APP_ARM64)[:40],
            "garbage": bytes(junk.getrandbits(8) for _ in range(4096)),
            "text": b"#!/bin/sh\necho Runner\n" * 50,
            "java class": b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + bytes(200),
            "fat, no slices": struct.pack(">II", 0xCAFEBABE, 0),
            "fat, slice past the end": struct.pack(">IIIIIII", 0xCAFEBABE, 1, CPU_ARM64, 0,
                                                   1 << 20, 4096, 4),
            "fat, slice in the header": struct.pack(">IIIIIII", 0xCAFEBABE, 1, CPU_ARM64, 0,
                                                    8, 4096, 4) + thin(APP_ARM64),
            "huge command count": thin(APP_ARM64, ncmds=1 << 30),
            "zero-sized command": thin(APP_ARM64)[:32] + struct.pack("<II", LC_SEGMENT_64, 0)
            + bytes(200),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(ios_simctl.binary_uuids(self.write(name, data)), set())

    def test_a_truncated_binary_never_yields_part_of_its_set(self) -> None:
        whole = app_binary()
        path = self.root / "cut"
        results = set()
        for length in range(len(whole)):
            path.write_bytes(whole[:length])
            found = ios_simctl.binary_uuids(path)
            self.assertIn(found, (set(), {APP_X86_64, APP_ARM64}), f"cut at {length}")
            results.add(frozenset(found))
        self.assertIn(frozenset(), results)

    def test_what_is_not_a_regular_file_is_never_opened(self) -> None:
        self.assertEqual(ios_simctl.binary_uuids(self.root / "missing"), set())
        self.assertEqual(ios_simctl.binary_uuids(self.root), set())
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo"
            os.mkfifo(fifo)
            started = time.monotonic()
            self.assertEqual(ios_simctl.binary_uuids(fifo), set())
            self.assertLess(time.monotonic() - started, 5, "a FIFO must not block the read")

    def test_reads_are_bounded(self) -> None:
        """Only the headers and load commands are read, never the code; a
        header that claims a huge command area is read up to the cap."""
        real_open = open
        counted = [0]

        class Counting:
            def __init__(self, handle: Any) -> None:
                self.handle = handle

            def read(self, size: int = -1) -> bytes:
                data = self.handle.read(size)
                counted[0] += len(data)
                return data

            def seek(self, *args: Any) -> int:
                return self.handle.seek(*args)

            def __enter__(self) -> "Counting":
                return self

            def __exit__(self, *_exc: Any) -> None:
                self.handle.close()

        def counting_open(path: Any, *args: Any, **kwargs: Any) -> Counting:
            return Counting(real_open(path, *args, **kwargs))

        big = self.write("big", fat([(CPU_X86_64, thin(APP_X86_64, cputype=CPU_X86_64,
                                                          payload=4 << 20)),
                                     (CPU_ARM64, thin(APP_ARM64, payload=4 << 20))]))
        claims = self.write("claims", thin(APP_ARM64, sizeofcmds=0xFFFFFFFF, payload=3 << 20))
        with mock.patch.object(ios_simctl, "open", counting_open, create=True):
            self.assertEqual(ios_simctl.binary_uuids(big), {APP_X86_64, APP_ARM64})
            self.assertLess(counted[0], 16 * 1024, "the code must not be read")
            counted[0] = 0
            self.assertEqual(ios_simctl.binary_uuids(claims), {APP_ARM64})
            self.assertLessEqual(counted[0], ios_simctl.MACHO_MAX_COMMAND_BYTES + 64)


HOST_BINARY = Path("/usr/bin/true")


def _host_macho() -> bool:
    try:
        with HOST_BINARY.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in ios_simctl._MACHO_THIN or magic in ios_simctl._MACHO_FAT  # noqa: SLF001


class HostBinaryTests(unittest.TestCase):
    """A real Mach-O on this host, when there is one: never required."""

    def setUp(self) -> None:
        if not _host_macho():
            self.skipTest(f"{HOST_BINARY} is not a Mach-O binary on this host")

    def test_a_host_binary_has_canonical_uuids(self) -> None:
        found = ios_simctl.binary_uuids(HOST_BINARY)
        self.assertTrue(found)
        for value in found:
            self.assertRegex(value, CANONICAL)

    def test_a_host_binary_matches_dwarfdump(self) -> None:
        dwarfdump = shutil.which("dwarfdump")
        if not dwarfdump:
            self.skipTest("dwarfdump is not installed")
        try:
            completed = subprocess.run([dwarfdump, "--uuid", str(HOST_BINARY)],
                                       capture_output=True, text=True, timeout=60,
                                       check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f"dwarfdump could not run: {exc}")
        expected = set(re.findall(r"UUID: ([0-9A-Fa-f-]{36})", completed.stdout or ""))
        if completed.returncode != 0 or not expected:
            self.skipTest("dwarfdump printed no UUID (no developer tools?)")
        self.assertEqual(ios_simctl.binary_uuids(HOST_BINARY),
                         {value.upper() for value in expected})


class ImageUuidCacheTests(_Tmp):
    def test_cached_while_unchanged_read_again_when_replaced_dropped_when_gone(self) -> None:
        binary = self.write("app/Runner", app_binary())
        with mock.patch.object(ios_simctl, "binary_uuids",
                               wraps=ios_simctl.binary_uuids) as read:
            self.assertEqual(ios_simctl.image_uuids(binary), {APP_X86_64, APP_ARM64})
            self.assertEqual(ios_simctl.image_uuids(binary), {APP_X86_64, APP_ARM64})
            self.assertEqual(read.call_count, 1, "an unchanged binary is not read twice")
            replacement = self.write("next", thin(NEXT_BUILD, payload=64))
            os.replace(replacement, binary)
            self.assertEqual(ios_simctl.image_uuids(binary), {NEXT_BUILD})
            binary.unlink()
            self.assertEqual(ios_simctl.image_uuids(binary), frozenset())
        self.assertNotIn(str(binary), ios_simctl._UUID_CACHE)  # noqa: SLF001

    def test_an_unreadable_plist_still_falls_back_to_the_executable(self) -> None:
        bundle = self.root / "Bundle/Application/B/Knit.app"
        self.write("Bundle/Application/B/Knit.app/Runner", app_binary())
        self.assertEqual(ios_simctl.installed_image_uuids(str(bundle), "Runner", BUNDLE),
                         {APP_X86_64, APP_ARM64}, "no plist: the executable decides")
        (bundle / "Info.plist").write_text("<?xml version='1.0'?><plist><dict><key>CFBundle",
                                           encoding="utf-8")
        self.assertEqual(ios_simctl.installed_image_uuids(str(bundle), "Runner", BUNDLE),
                         {APP_X86_64, APP_ARM64}, "an unreadable plist: the same")
        (bundle / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Other"}))
        self.assertEqual(ios_simctl.installed_image_uuids(str(bundle), "Runner", BUNDLE),
                         {APP_X86_64, APP_ARM64}, "a plist naming no identifier: the same")

    def test_the_installed_bundle_names_its_binary(self) -> None:
        bundle = self.root / "Bundle/Application/A/Knit.app"
        self.write("Bundle/Application/A/Knit.app/KnitBin", app_binary())
        self.assertEqual(ios_simctl.installed_image_uuids(str(bundle), "KnitBin"),
                         {APP_X86_64, APP_ARM64})
        (bundle / "Info.plist").write_bytes(
            b'<?xml version="1.0"?><plist version="1.0"><dict>'
            b"<key>CFBundleIdentifier</key><string>com.example.knit</string>"
            b"<key>CFBundleExecutable</key><string>KnitBin</string></dict></plist>")
        # the installed Info.plist wins over a guessed name
        self.assertEqual(ios_simctl.installed_image_uuids(str(bundle), "Runner", BUNDLE),
                         {APP_X86_64, APP_ARM64})
        self.assertEqual(ios_simctl.installed_image_uuids(None, "Runner"), frozenset())
        self.assertEqual(ios_simctl.installed_image_uuids(str(bundle / "nope"), "Runner"),
                         frozenset())


# --- 2. the record filter and the predicate -------------------------------------------

FIRST = "/Sim/Containers/Bundle/Application/FE85D28A/Runner-26.09.23.app"
CURRENT = "/Sim/Containers/Bundle/Application/441D5220/Runner-26.09.23.app"
THEIRS = "/Sim/Containers/Bundle/Application/B0B0B0B0/Runner.app"


class RecordFilterTests(unittest.TestCase):
    """`log_line_matches` with the installed binary's UUIDs."""

    UUIDS = [APP_ARM64.lower(), APP_X86_64]  # case-insensitive, any iterable

    def match(self, line: str, **kwargs: Any) -> bool:
        options: dict[str, Any] = {"executable": "Runner", "bundle_path": CURRENT,
                                   "uuids": self.UUIDS}
        options.update(kwargs)
        return ios_simctl.log_line_matches(line, BUNDLE, **options)

    def test_a_stale_path_with_the_apps_uuid_passes(self) -> None:
        """(a) the defect: every record named the first, deleted container."""
        stale = record_line(f"{FIRST}/Runner", "flutter: cart updated", uuid=APP_ARM64)
        engine = record_line(f"{FIRST}/Runner", "engine", uuid=APP_ARM64,
                             sender=f"{FIRST}/Frameworks/Flutter.framework/Flutter",
                             sender_uuid=FLUTTER_ENGINE)
        by_sender = record_line("/usr/libexec/somehost", "x", uuid=OTHER_APP,
                                sender_uuid=APP_X86_64)
        for line in (stale, engine, by_sender):
            with self.subTest(line=line):
                self.assertTrue(self.match(line))
        # before: without UUIDs, the bundle path decided — and rejected them
        self.assertFalse(self.match(stale, uuids=None))

    def test_another_runner_is_rejected_even_under_the_apps_path(self) -> None:
        """(b) another Flutter app is a `Runner` too; its UUID decides."""
        theirs = record_line(f"{THEIRS}/Runner", "flutter: someone else", uuid=OTHER_APP)
        disguised = record_line(f"{CURRENT}/Runner", f"opening {BUNDLE}", uuid=OTHER_APP)
        self.assertFalse(self.match(theirs))
        self.assertFalse(self.match(disguised))
        self.assertTrue(self.match(disguised, uuids=None), "the path alone would pass it")

    def test_the_subsystem_passes_whatever_the_image(self) -> None:
        """(c)"""
        line = record_line("/usr/libexec/locationd", "s", uuid=OTHER_APP, subsystem=BUNDLE)
        self.assertTrue(self.match(line))

    def test_without_uuids_the_path_rules_are_unchanged(self) -> None:
        """(d) no UUIDs known (unreadable binary, nothing installed)."""
        current = record_line(f"{CURRENT}/Runner", "m", uuid=APP_ARM64)
        stale = record_line(f"{FIRST}/Runner", "m", uuid=APP_ARM64)
        for uuids in (None, [], set()):
            with self.subTest(uuids=uuids):
                self.assertTrue(self.match(current, uuids=uuids))
                self.assertFalse(self.match(stale, uuids=uuids))
                self.assertTrue(self.match(stale, uuids=uuids, bundle_path=None))
        # a record that names no UUID falls back to the path rules too
        bare = json.dumps({"processImagePath": f"{CURRENT}/Runner", "eventMessage": "m"})
        self.assertTrue(self.match(bare))
        self.assertFalse(self.match(json.dumps({"processImagePath": f"{FIRST}/Runner"})))

    def test_plain_text_lines_and_noise(self) -> None:
        self.assertTrue(self.match(f"12:00 {CURRENT}/Runner hello"))
        self.assertFalse(self.match(f"12:00 {FIRST}/Runner hello"))
        self.assertFalse(self.match('{"count":3,"finished":1}'))
        self.assertFalse(self.match('Filtering the log data using "subsystem == \\"x\\""'))


class PredicateTests(unittest.TestCase):
    def test_the_server_predicate_names_the_executable_not_the_container(self) -> None:
        predicate = ios_simctl.log_predicate(BUNDLE, executable="Runner", bundle_path=CURRENT)
        self.assertEqual(predicate, f'subsystem == "{BUNDLE}" OR '
                                    'processImagePath ENDSWITH "/Runner" OR '
                                    'senderImagePath ENDSWITH "/Runner"')

    def test_without_the_executable_the_distinctive_leaf_is_kept(self) -> None:
        self.assertEqual(ios_simctl.log_predicate(BUNDLE, bundle_path=CURRENT),
                         f'subsystem == "{BUNDLE}" OR processImagePath CONTAINS "knit" OR '
                         'senderImagePath CONTAINS "knit"')
        self.assertEqual(ios_simctl.log_predicate("com.example.app", bundle_path=CURRENT),
                         'subsystem == "com.example.app"')

    def test_the_stream_argv_carries_it(self) -> None:
        argv = ios_simctl.log_stream_argv("/x/xcrun", UDID, bundle_id=BUNDLE,
                                          executable="Runner", bundle_path=CURRENT)
        predicate = argv[argv.index("--predicate") + 1]
        self.assertNotIn("BEGINSWITH", predicate)
        self.assertNotIn(CURRENT, predicate)


# --- 3. through the fake simctl: resolution, tail, follow, the session writer ----------


class _Fake(_Tmp):
    """The fake simctl with an installed app whose binary is real Mach-O."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.set_env(AUTONOM_HOME=str(self.root / "home"), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_SIMCTL=None,
                     AUTONOM_IOS_LOG_MAX_MB=None)
        self.target = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})
        apps = self.root / "Containers/Bundle/Application"
        self.first = apps / "FE85D28A/Runner-26.09.23.app"  # deleted: never created
        self.other = apps / "B0B0B0B0/Runner.app"
        self.other.mkdir(parents=True)
        (self.other / "Runner").write_bytes(thin(OTHER_APP))
        self.bundle = self.install("441D5220")
        self.mine = record_line(f"{self.first}/Runner", "flutter: mine", uuid=APP_ARM64)
        self.engine = record_line(f"{self.first}/Runner", "flutter engine", uuid=APP_ARM64,
                                  sender=f"{self.first}/Frameworks/Flutter.framework/Flutter",
                                  sender_uuid=FLUTTER_ENGINE)
        self.theirs = record_line(f"{self.other}/Runner", "flutter: theirs", uuid=OTHER_APP)
        self.lines = ['Filtering the log data using "..."', self.mine, self.theirs,
                      self.engine, '{"count":3,"finished":1}']
        self.write_state()

    def install(self, container: str, binary: bytes | None = None) -> Path:
        """The app installed at a (new) container: what a reinstall does."""
        bundle = self.root / "Containers/Bundle/Application" / container / "Runner-26.09.23.app"
        bundle.mkdir(parents=True)
        (bundle / "Runner").write_bytes(binary or app_binary())
        self.bundle = bundle
        return bundle

    def write_state(self, **extra: Any) -> None:
        state = {
            "simctl_devices": {"devices": {RUNTIME: [{"udid": UDID, "name": "iPhone 17 Pro",
                                                      "state": "Booted", "isAvailable": True}]}},
            "app_info": {BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(self.bundle)}},
            "app_bundle": str(self.bundle), "installed": [BUNDLE],
            "install_bundle_id": BUNDLE, "running": [BUNDLE], "ios_log": self.lines,
        }
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    @staticmethod
    def messages(entries: list[dict[str, Any]]) -> list[str]:
        """The event messages of `logs tail` entries (compact lines read
        `<timestamp> <image> Default <message>`)."""
        return [entry["line"].split(" Default ", 1)[-1] for entry in entries]

    def stream_file(self, record: dict[str, Any], lines: list[str]) -> Path:
        stream = logs.stream_destination(record)
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return stream


class ResolutionTests(_Fake):
    def test_app_identity_reads_the_installed_binary(self) -> None:
        self.assertEqual(ios_simctl.app_identity(str(FAKE_SIMCTL), UDID, BUNDLE),
                         ("Runner", str(self.bundle), {APP_X86_64, APP_ARM64}))

    def test_a_reinstall_of_another_build_is_read_again(self) -> None:
        ios_simctl.app_identity(str(FAKE_SIMCTL), UDID, BUNDLE)
        shutil.rmtree(self.bundle.parent)
        self.install("7E7E7E7E", thin(NEXT_BUILD))
        self.write_state()
        self.assertEqual(ios_simctl.app_identity(str(FAKE_SIMCTL), UDID, BUNDLE),
                         ("Runner", str(self.bundle), {NEXT_BUILD}))

    def test_the_predicate_never_names_the_container(self) -> None:
        predicate = ios_simctl.app_log_predicate(str(FAKE_SIMCTL), UDID, BUNDLE)
        self.assertEqual(predicate, ios_simctl.log_predicate(BUNDLE, executable="Runner"))
        self.assertNotIn(str(self.bundle.parent), predicate)

    def test_the_filter_follows_the_uuid_not_the_path(self) -> None:
        keep = ios_simctl.app_log_filter(str(FAKE_SIMCTL), UDID, BUNDLE)
        self.assertEqual([keep(line) for line in self.lines],
                         [False, True, False, True, False])
        # UUIDs a session recorded for an earlier build pass too
        old = record_line(f"{self.first}/Runner", "old build", uuid=NEXT_BUILD)
        self.assertFalse(keep(old))
        keep = ios_simctl.app_log_filter(str(FAKE_SIMCTL), UDID, BUNDLE,
                                         extra_uuids=[NEXT_BUILD.lower()])
        self.assertTrue(keep(old))


class TailTests(_Fake):
    def session(self, app_id: str = BUNDLE, uuids: list[str] | None = None) -> dict[str, Any]:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=app_id)
        if uuids is not None:
            record["background"]["log_stream_image_uuids"] = uuids
        session.save(record)
        return record

    def test_the_stream_file_keeps_the_apps_records_at_a_stale_path(self) -> None:
        """Before: the bundle path `441D5220/...` was matched; every record
        said `FE85D28A/...`, so the tail was empty."""
        stream = self.stream_file(self.session(), self.lines)
        detail = logs.tail_detailed(self.target, stream_path=stream, package=BUNDLE)
        self.assertEqual(self.messages(detail["entries"]), ["flutter: mine", "flutter engine"])
        self.assertEqual(detail["executable"], "Runner")
        self.assertEqual(detail["image_uuids"], sorted({APP_X86_64, APP_ARM64}))
        # without --package, the session's own stream is read as its app
        bare = logs.tail_detailed(self.target, stream_path=stream)
        self.assertEqual((bare["entries"], bare["image_uuids"]),
                         (detail["entries"], detail["image_uuids"]))

    def test_the_apps_records_are_found_among_many_others(self) -> None:
        """The stream holds every `Runner`: the app's lines are found even
        when the last lines are all another app's."""
        stream = self.stream_file(self.session(), [self.mine] + [self.theirs] * 900)
        detail = logs.tail_detailed(self.target, stream_path=stream, package=BUNDLE,
                                    max_lines=10)
        self.assertEqual(self.messages(detail["entries"]), ["flutter: mine"])

    def test_log_show_is_narrowed_on_the_client(self) -> None:
        """`log show` hands over every app with the executable's name (the
        fake prints them all); before, only the predicate narrowed it."""
        detail = logs.tail_detailed(self.target, package=BUNDLE)
        self.assertEqual(self.messages(detail["entries"]), ["flutter: mine", "flutter engine"])
        show = [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines()
                if "show" in json.loads(line)["argv"]]
        predicate = show[-1][show[-1].index("--predicate") + 1]
        self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)
        self.assertNotIn("BEGINSWITH", predicate)

    def test_the_recorded_uuids_outlive_the_app(self) -> None:
        record = self.session(uuids=[APP_ARM64])
        stream = self.stream_file(record, self.lines)
        shutil.rmtree(self.bundle.parent)  # uninstalled
        self.write_state(app_info={}, app_bundle="(null)", installed=[])
        detail = logs.tail_detailed(self.target, stream_path=stream, package=BUNDLE)
        self.assertEqual(self.messages(detail["entries"]), ["flutter: mine", "flutter engine"])

    def test_another_apps_recorded_uuids_vouch_for_nothing(self) -> None:
        record = self.session(app_id=OTHER_BUNDLE, uuids=[OTHER_APP])
        stream = self.stream_file(record, self.lines)
        detail = logs.tail_detailed(self.target, stream_path=stream, package=BUNDLE)
        self.assertNotIn("flutter: theirs", self.messages(detail["entries"]))


class FollowTests(_Fake):
    def test_live_and_file_follow_filters(self) -> None:
        keep = logs.ios_line_filter(self.target, BUNDLE)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        argv = logs.ios_follow_argv(self.target, BUNDLE)
        self.assertIn('processImagePath ENDSWITH "/Runner"', argv[argv.index("--predicate") + 1])

    def test_a_session_record_adds_its_recorded_uuids(self) -> None:
        old = record_line(f"{self.first}/Runner", "old build", uuid=NEXT_BUILD)
        record = {"app_id": BUNDLE, "background": {"log_stream_image_uuids": [NEXT_BUILD]}}
        self.assertFalse(logs.ios_line_filter(self.target, BUNDLE)(old))
        self.assertTrue(logs.ios_line_filter(self.target, BUNDLE, record=record)(old))
        record["app_id"] = OTHER_BUNDLE
        self.assertFalse(logs.ios_line_filter(self.target, BUNDLE, record=record)(old))

    def test_a_past_sessions_replay_needs_no_device(self) -> None:
        record = {"app_id": BUNDLE, "background": {"log_stream_executable": "Runner",
                                                   "log_stream_image_uuids": [APP_ARM64]}}
        keep = logs.recorded_line_filter(record, BUNDLE)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        # nothing recorded: the recorded executable decides, as before
        record["background"].pop("log_stream_image_uuids")
        keep = logs.recorded_line_filter(record, BUNDLE)
        self.assertEqual([keep(line) for line in self.lines], [False, True, True, True, False])
        # no package: only the log tool's chatter is dropped
        keep = logs.recorded_line_filter(record, None)
        self.assertEqual([keep(line) for line in self.lines], [False, True, True, True, False])

    def test_a_replay_of_another_package_is_resolved_fresh(self) -> None:
        record = {"app_id": OTHER_BUNDLE,
                  "background": {"log_stream_executable": "Runner",
                                 "log_stream_image_uuids": [OTHER_APP]}}
        keep = logs.recorded_line_filter(record, BUNDLE, target=self.target)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertEqual(logs.filter_image_uuids(keep), sorted({APP_X86_64, APP_ARM64}))
        own = logs.recorded_line_filter({**record, "app_id": BUNDLE}, BUNDLE,
                                        target=self.target)
        self.assertEqual(logs.filter_image_uuids(own), [OTHER_APP], "the recording decides")
        self.assertEqual(logs.filter_image_uuids(logs.recorded_line_filter(record, None)), [])


class SessionStreamDefaultTests(_Fake):
    """Which files a package-less read narrows to the session's app."""

    def test_only_the_sessions_stream_and_its_rotation_are_the_session_stream(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        logs_dir = Path(record["artifacts_dir"]) / "logs"
        for name, expected in (("stream.ndjson", True), ("stream.ndjson.1", True),
                               ("stream.ndjson.bak", False), ("stream.ndjson.2", False),
                               ("copy.ndjson", False), ("latest.json", False)):
            with self.subTest(name=name):
                self.assertIs(logs.is_session_stream(record, logs_dir / name), expected)
        elsewhere = self.root / "logs" / "stream.ndjson"
        self.assertFalse(logs.is_session_stream(record, elsewhere))
        link = self.root / "alias"
        link.symlink_to(logs_dir, target_is_directory=True)
        self.assertTrue(logs.is_session_stream(record, link / "stream.ndjson"))

    def test_the_same_file_under_another_name_is_the_session_stream(self) -> None:
        """A hard link, or a case variant on a case-insensitive volume, is
        the stream itself: it used to be read raw."""
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        stream = self.stream_file(record, [self.mine])
        hard = stream.with_name("hard.ndjson")
        os.link(stream, hard)
        self.assertTrue(logs.is_session_stream(record, hard))
        copy = stream.with_name("copy.ndjson")
        shutil.copyfile(stream, copy)
        self.assertFalse(logs.is_session_stream(record, copy), "a copy is another file")
        rotated = stream.with_name("stream.ndjson.1")
        rotated.write_text(self.mine + "\n", encoding="utf-8")
        os.link(rotated, stream.with_name("old.ndjson"))
        self.assertTrue(logs.is_session_stream(record, stream.with_name("old.ndjson")))

    def test_a_case_variant_is_the_session_stream_where_the_volume_ignores_case(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        stream = self.stream_file(record, [self.mine])
        upper = stream.with_name("STREAM.ndjson")
        if not upper.exists():
            self.skipTest("this volume is case-sensitive")
        self.assertTrue(logs.is_session_stream(record, upper))
        self.assertTrue(logs.is_session_stream(record, stream.parent.parent / "LOGS" /
                                               "Stream.NDJSON"))

    def test_the_default_app_needs_an_ios_session_with_an_app_and_an_identity(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        stream = logs.stream_destination(record)
        self.assertIsNone(logs.default_stream_app(record, stream), "nothing recorded")
        self.assertEqual(logs.default_stream_app(record, stream, target=self.target), BUNDLE)
        other = Target(IOS, "BBBBBBBB-0000-0000-0000-000000000000", str(FAKE_SIMCTL))
        self.assertIsNone(logs.default_stream_app(record, stream, target=other),
                          "another simulator says nothing about this session's app")
        record["background"]["log_stream_executable"] = "Runner"
        self.assertEqual(logs.default_stream_app(record, stream), BUNDLE)
        self.assertIsNone(logs.default_stream_app({**record, "app_id": None}, stream))
        self.assertIsNone(logs.default_stream_app({**record, "platform": "android"}, stream))
        keep = logs.session_stream_filter(record, stream, target=self.target)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertIsNone(logs.session_stream_filter(record, self.root / "x.ndjson",
                                                     target=self.target))


class SessionStreamProbeTests(_Fake):
    """A package-less read of the current session's file asks the simulator
    only when that can add something, and only briefly."""

    def session_record(self, **background: Any) -> dict[str, Any]:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        record["background"].update(background)
        session.save(record)
        self.stream_file(record, self.lines)
        return record

    def appinfo_calls(self) -> int:
        if not self.log.exists():
            return 0
        return sum(1 for line in self.log.read_text(encoding="utf-8").splitlines()
                   if "appinfo" in json.loads(line)["argv"])

    def test_the_recorded_install_on_disk_needs_no_device(self) -> None:
        info_plist(self.bundle, BUNDLE)
        record = self.session_record(log_stream_executable="Runner",
                                     log_stream_image_uuids=[APP_ARM64],
                                     log_stream_bundle_path=str(self.bundle))
        keep = logs.session_stream_filter(record, logs.stream_destination(record),
                                          target=self.target)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertEqual(self.appinfo_calls(), 0)
        self.assertEqual(logs.filter_image_uuids(keep), sorted({APP_X86_64, APP_ARM64}),
                         "the binary on disk is read again")

    def test_a_recorded_install_reused_by_another_app_vouches_for_nothing(self) -> None:
        """The recorded path now holds another app's `Runner` (its
        Info.plist names that app): its binary's UUIDs used to be trusted,
        so the other app's records passed as the session app's."""
        info_plist(self.other, OTHER_BUNDLE)
        record = self.session_record(log_stream_executable="Runner",
                                     log_stream_image_uuids=[APP_ARM64],
                                     log_stream_bundle_path=str(self.other))
        stream = logs.stream_destination(record)
        # the current session: treated as gone, so the installed app is asked
        keep = logs.session_stream_filter(record, stream, target=self.target)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertEqual(logs.filter_image_uuids(keep), sorted({APP_X86_64, APP_ARM64}))
        self.assertEqual(self.appinfo_calls(), 1)
        # a replay: the recorded identity alone, no device
        keep = logs.session_stream_filter(record, stream)
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertEqual(logs.filter_image_uuids(keep), [APP_ARM64])
        self.assertEqual(self.appinfo_calls(), 1)
        self.assertEqual(ios_simctl.installed_image_uuids(str(self.other), "Runner", BUNDLE),
                         frozenset(), "another app's install lends no UUIDs")
        self.assertEqual(ios_simctl.bundle_identifier(self.other), OTHER_BUNDLE)
        self.assertEqual(ios_simctl.installed_image_uuids(str(self.other), "Runner",
                                                          OTHER_BUNDLE), {OTHER_APP})

    def test_a_reinstall_is_asked_for_and_adds_the_new_build(self) -> None:
        record = self.session_record(log_stream_executable="Runner",
                                     log_stream_image_uuids=[APP_ARM64],
                                     log_stream_bundle_path=str(self.bundle))
        shutil.rmtree(self.bundle.parent)
        self.install("7E7E7E7E", thin(NEXT_BUILD))
        self.write_state()
        keep = logs.session_stream_filter(record, logs.stream_destination(record),
                                          target=self.target)
        self.assertEqual(self.appinfo_calls(), 1)
        self.assertEqual(logs.filter_image_uuids(keep), sorted({APP_ARM64, NEXT_BUILD}))
        self.assertTrue(keep(record_line(f"{self.first}/Runner", "new", uuid=NEXT_BUILD)))

    def test_a_hanging_probe_keeps_the_recorded_identity_quietly(self) -> None:
        record = self.session_record(log_stream_executable="Runner",
                                     log_stream_image_uuids=[APP_ARM64])
        self.write_state(simctl_hang={"simctl appinfo": 30, "simctl get_app_container": 30})
        started = time.monotonic()
        with mock.patch.object(logs, "SESSION_PROBE_TIMEOUT", 0.5, create=True):
            keep = logs.session_stream_filter(record, logs.stream_destination(record),
                                              target=self.target)
        self.assertLess(time.monotonic() - started, 5, "the probe was not bounded")
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertEqual(logs.filter_image_uuids(keep), [APP_ARM64])

    def test_a_replay_never_asks_the_device(self) -> None:
        record = self.session_record(log_stream_executable="Runner",
                                     log_stream_image_uuids=[APP_ARM64])
        keep = logs.session_stream_filter(record, logs.stream_destination(record))
        self.assertEqual([keep(line) for line in self.lines], [False, True, False, True, False])
        self.assertEqual(self.appinfo_calls(), 0)


class SessionWriterTests(_Fake):
    def test_the_session_records_the_binary_uuids(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        record["background"]["log_stream_image_uuids"] = [NEXT_BUILD]  # an earlier build
        captured: dict[str, Any] = {}

        def fake_start(_xcrun, _udid, _destination, **kwargs):
            captured.update(kwargs)
            return None

        with mock.patch.object(ios_simctl, "start_log_stream", fake_start):
            logs.start_session_log_stream(self.target, record)
        self.assertEqual(record["background"]["log_stream_image_uuids"],
                         sorted({NEXT_BUILD, APP_X86_64, APP_ARM64}))
        self.assertEqual(record["background"]["log_stream_executable"], "Runner")
        self.assertEqual(captured["executable"], "Runner")

    def test_the_record_holds_the_identity_the_writer_used(self) -> None:
        """A first lookup that misses and a later one that hits (misses are
        not cached): the writer's predicate named `Runner` while the record
        held no identity, so its stream could not be read as the app."""
        for misses in (0, 1, 2):
            with self.subTest(misses=misses):
                ios_simctl.reset_caches()
                record = session.start_session(str(FAKE_SIMCTL), platform="ios",
                                               target_id=UDID, app_id=BUNDLE)
                answers = ([(None, None)] * misses) + [("Runner", str(self.bundle))] * 5
                spawned: list[list[str]] = []

                def spawn(argv: Any, destination: Any, **_kwargs: Any) -> Any:
                    spawned.append(list(argv))
                    return mock.Mock(pid=None)  # nothing runs, nothing registered

                with mock.patch.object(ios_simctl, "app_image", side_effect=answers), \
                        mock.patch.object(ios_simctl, "spawn_bounded", spawn):
                    logs.start_session_log_stream(self.target, record)
                predicate = spawned[-1][spawned[-1].index("--predicate") + 1]
                background = record["background"]
                if misses < 2:
                    self.assertIn('ENDSWITH "/Runner"', predicate)
                    self.assertEqual(background["log_stream_executable"], "Runner")
                    self.assertEqual(background["log_stream_image_uuids"],
                                     sorted({APP_X86_64, APP_ARM64}))
                    self.assertEqual(background["log_stream_bundle_path"], str(self.bundle))
                else:  # every lookup missed: neither names the app's image
                    self.assertNotIn("Runner", predicate)
                    self.assertNotIn("log_stream_executable", background)

    def test_the_real_writer_writes_every_runner_and_the_banner_never(self) -> None:
        destination = self.root / "out" / "stream.ndjson"
        spawned: list[subprocess.Popen] = []
        real_spawn = ios_simctl.spawn_bounded

        def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen:
            spawned.append(real_spawn(*args, **kwargs))
            return spawned[-1]

        with mock.patch.object(ios_simctl, "spawn_bounded", spawn):
            pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, destination,
                                              bundle_id=BUNDLE)
        (writer,) = spawned
        self.assertEqual(pid, writer.pid)
        writer.wait(timeout=60)  # reaped through its Popen: no ResourceWarning
        self.assertEqual(destination.read_text(encoding="utf-8").splitlines(),
                         [self.mine, self.theirs, self.engine],
                         "raw on disk; narrowed when read")


# --- 4. end to end: session start --log-stream, a reinstall, logs tail -----------------


class _Cli(_Fake):
    """The CLI against the fakes, with a private home and PATH."""

    def setUp(self) -> None:
        super().setUp()
        self.set_env(AUTONOM_ADB=None, AUTONOM_IDB=None, AUTONOM_MITMDUMP=None,
                     AUTONOM_AXE=None, AUTONOM_IOS_HID="idb", AUTONOM_IDB_COMPANION=None,
                     AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
                     AUTONOM_CORESIMULATOR_DEVICES=str(self.root / "Devices"),
                     DEVELOPER_DIR=None)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
        self.build = self.root / "build/ios/iphonesimulator/Runner.app"
        self.build.mkdir(parents=True)
        self.addCleanup(self.stop_session)

    def cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", UDID, *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def stop_session(self) -> None:
        if (self.root / "home").exists():
            subprocess.run([sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--udid",
                            UDID, "session", "stop"], cwd=self.root, env=self.env,
                           capture_output=True, check=False, timeout=120)

    def cli_stream(self, *argv: str) -> tuple[int, list[dict]]:
        """A streaming verb: its NDJSON lines."""
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", UDID, *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed.returncode, [json.loads(line) for line in
                                      completed.stdout.splitlines() if line.strip()]

    def stream_predicates(self) -> list[str]:
        streams = [json.loads(line)["argv"] for line in
                   self.log.read_text(encoding="utf-8").splitlines()
                   if "stream" in json.loads(line)["argv"]]
        return [argv[argv.index("--predicate") + 1] for argv in streams]

    def calls(self) -> list[list[str]]:
        """Every simctl argv the fake received."""
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def followed(lines: list[dict]) -> list[str]:
        """The event messages a follow emitted."""
        return [json.loads(line["text"])["eventMessage"]
                for line in lines if line.get("kind") == "line"]

    @staticmethod
    def eof(lines: list[dict]) -> dict:
        (last,) = [line for line in lines if line.get("kind") == "eof"]
        return last

    def sleeper(self) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 stdin=subprocess.DEVNULL)

        def stop() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

        self.addCleanup(stop)
        return child


class CliReinstallTests(_Cli):
    def test_log_stream_and_tail_after_a_reinstall_return_the_apps_lines(self) -> None:
        """Before: `--log-stream` filtered on the installed bundle path, the
        log named the first container, and `logs tail --package` answered
        `count: 0` while the app ran."""
        code, started = self.cli("session", "start", "--app-id", BUNDLE, "--install",
                                 str(self.build), "--log-stream")
        self.assertEqual(code, 0, started)
        background = started["session"]["background"]
        self.assertTrue(_wait_until(lambda: _gone(background["log_stream_pid"])))
        # the reinstall moves the app to a new container (the same build)
        shutil.rmtree(self.bundle.parent)
        self.install("0DDC0FFE")
        self.write_state()
        code, cleared = self.cli("session", "clear", BUNDLE, "--strategy", "reinstall")
        self.assertEqual(code, 0, cleared)
        self.assertIs(cleared["log_stream_restarted"], True)
        self.assertTrue(_wait_until(lambda: _gone(cleared["log_stream_pid"])))

        code, tail = self.cli("logs", "tail", "--package", BUNDLE)
        self.assertEqual(code, 0, tail)
        self.assertEqual(self.messages(tail["lines"]),
                         ["flutter: mine", "flutter engine"] * 2)
        self.assertEqual(tail["executable"], "Runner")
        self.assertEqual(tail["image_uuids"], sorted({APP_X86_64, APP_ARM64}))
        for predicate in self.stream_predicates():
            self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)
            self.assertNotIn("Containers/Bundle", predicate)
        # what the stream's records are matched by on read is on the record
        self.assertEqual(background["log_stream_image_uuids"], sorted({APP_X86_64, APP_ARM64}))

    def test_tail_without_a_stream_after_a_reinstall(self) -> None:
        shutil.rmtree(self.bundle.parent)
        self.install("0DDC0FFE")
        self.write_state()
        code, tail = self.cli("logs", "tail", "--package", BUNDLE)
        self.assertEqual(code, 0, tail)
        self.assertEqual(self.messages(tail["lines"]), ["flutter: mine", "flutter engine"])
        self.assertEqual(tail["image_uuids"], sorted({APP_X86_64, APP_ARM64}))

    def test_tail_names_no_uuids_when_none_were_used(self) -> None:
        shutil.rmtree(self.bundle.parent)
        self.write_state(app_info={}, app_bundle="(null)")
        code, tail = self.cli("logs", "tail", "--package", BUNDLE)
        self.assertEqual(code, 0, tail)
        self.assertNotIn("image_uuids", tail)


class CliFollowTests(_Cli):
    """`logs follow --source device --package` with another `Runner` app's
    records present: live, from the current session's stream, and from a
    past session's recording. Before the wiring, the live follow dropped
    only the `log` tool's chatter, the session follow ignored the UUIDs the
    session recorded, and the replay matched the recorded executable —
    which every Flutter app shares."""

    def old_build(self) -> str:
        return record_line(f"{self.first}/Runner", "old build", uuid=NEXT_BUILD)

    def test_live_follow_keeps_only_the_apps_records(self) -> None:
        """C: no session stream, so a live `log stream` (the fake prints
        every record the executable predicate would admit)."""
        code, lines = self.cli_stream("logs", "follow", "--source", "device", "--package",
                                      BUNDLE, "--max-seconds", "10")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.followed(lines), ["flutter: mine", "flutter engine"])
        self.assertEqual(self.eof(lines)["image_uuids"], sorted({APP_X86_64, APP_ARM64}))
        (predicate,) = self.stream_predicates()
        self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)

    def test_live_follow_without_a_package_drops_only_the_chatter(self) -> None:
        code, lines = self.cli_stream("logs", "follow", "--source", "device",
                                      "--max-seconds", "10")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.followed(lines),
                         ["flutter: mine", "flutter: theirs", "flutter engine"])
        self.assertNotIn("image_uuids", self.eof(lines))

    def test_session_stream_follow_uses_the_uuids_the_session_recorded(self) -> None:
        """B: the session's stream started on an earlier build (recorded
        UUID), and the app has since been replaced by another build: both
        builds' records are the app's, the other `Runner`'s are not."""
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        writer = self.sleeper()
        record["background"].update({"log_stream_pid": writer.pid,
                                     "log_stream_executable": "Runner",
                                     "log_stream_image_uuids": [NEXT_BUILD]})
        session.save(record)
        self.stream_file(record, [self.old_build(), self.theirs, self.mine])
        code, lines = self.cli_stream("logs", "follow", "--source", "device", "--package",
                                      BUNDLE, "--from-start", "--max-seconds", "2")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.followed(lines), ["old build", "flutter: mine"])
        self.assertEqual(self.eof(lines)["image_uuids"],
                         sorted({NEXT_BUILD, APP_X86_64, APP_ARM64}))

    def past_session(self, app_id: str, uuids: list[str]) -> dict[str, Any]:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=app_id)
        record["background"].update({"log_stream_executable": "Runner",
                                     "log_stream_image_uuids": uuids})
        session.save(record)
        self.stream_file(record, self.lines + [self.old_build()])
        session.stop_session(reap=False)
        return record

    def test_replay_matches_the_recorded_identity(self) -> None:
        """A: the session's own app, by the UUIDs its stream recorded — no
        device asked, the app is gone."""
        record = self.past_session(BUNDLE, [APP_ARM64, NEXT_BUILD])
        shutil.rmtree(self.bundle.parent)
        self.write_state(app_info={}, app_bundle="(null)", installed=[])
        code, lines = self.cli_stream("logs", "follow", "--source", "device", "--session-id",
                                      record["session_id"], "--package", BUNDLE,
                                      "--max-seconds", "2")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.followed(lines), ["flutter: mine", "flutter engine", "old build"])
        self.assertEqual(self.eof(lines)["image_uuids"], sorted({APP_ARM64, NEXT_BUILD}))

    def test_replay_of_another_package_resolves_its_installed_binary(self) -> None:
        """A: the recording is another app's; this package's identity comes
        from its installed binary, never from that app's recorded one."""
        record = self.past_session(OTHER_BUNDLE, [OTHER_APP])
        code, lines = self.cli_stream("logs", "follow", "--source", "device", "--session-id",
                                      record["session_id"], "--package", BUNDLE,
                                      "--max-seconds", "2")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.followed(lines), ["flutter: mine", "flutter engine"])
        self.assertEqual(self.eof(lines)["image_uuids"], sorted({APP_X86_64, APP_ARM64}))


class CliSessionStreamDefaultTests(_Cli):
    """A package-less read of an iOS session's own stream file. The writer's
    predicate names the executable, so the file holds every same-named app's
    raw records; before, `logs follow` (default source, `--source
    log_stream`, `--path logs/stream.ndjson`) and `logs tail` without
    `--package` printed another Flutter `Runner`'s lines with the app's."""

    def current_session(self, **background: Any) -> dict[str, Any]:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        record["background"].update(background)
        session.register_stream(record, stream_id="log_stream", kind="device_log",
                                path="logs/stream.ndjson", label="ios log stream", pid=None)
        session.save(record)
        self.stream_file(record, [self.theirs, self.mine])
        return record

    def follow(self, *argv: str) -> list[dict]:
        code, lines = self.cli_stream("logs", "follow", *argv, "--from-start",
                                      "--max-seconds", "2")
        self.assertEqual(code, 0, lines)
        return lines

    def assert_only_mine(self, lines: list[dict]) -> None:
        self.assertEqual(self.followed(lines), ["flutter: mine"])
        self.assertEqual(self.eof(lines)["image_uuids"], sorted({APP_X86_64, APP_ARM64}))

    def test_default_follow_reads_the_stream_as_the_sessions_app(self) -> None:
        """The reviewer's repro: a current session, [theirs, mine]."""
        self.current_session()
        self.assert_only_mine(self.follow())

    def test_source_log_stream_reads_it_as_the_sessions_app(self) -> None:
        self.current_session()
        self.assert_only_mine(self.follow("--source", "log_stream"))

    def test_path_to_the_stream_or_its_rotation_reads_it_as_the_sessions_app(self) -> None:
        record = self.current_session()
        self.assert_only_mine(self.follow("--path", "logs/stream.ndjson"))
        rotated = logs.stream_destination(record).with_name("stream.ndjson.1")
        rotated.write_text(f"{self.theirs}\n{self.mine}\n", encoding="utf-8")
        self.assert_only_mine(self.follow("--path", "logs/stream.ndjson.1"))

    def test_a_hard_link_to_the_stream_reads_it_as_the_sessions_app(self) -> None:
        record = self.current_session()
        os.link(logs.stream_destination(record),
                logs.stream_destination(record).with_name("hard.ndjson"))
        self.assert_only_mine(self.follow("--path", "logs/hard.ndjson"))

    def test_a_case_variant_reads_it_as_the_sessions_app(self) -> None:
        record = self.current_session()
        if not logs.stream_destination(record).with_name("STREAM.ndjson").exists():
            self.skipTest("this volume is case-sensitive")
        self.assert_only_mine(self.follow("--path", "logs/STREAM.ndjson"))

    def test_a_hanging_simulator_does_not_hold_up_a_file_follow(self) -> None:
        """Before: a 30 s `simctl appinfo` probe per lookup, where a plain
        file follow used to need no device at all."""
        self.current_session(log_stream_executable="Runner",
                             log_stream_image_uuids=[APP_ARM64])
        self.write_state(simctl_hang={"simctl appinfo": 60, "simctl get_app_container": 60})
        started = time.monotonic()
        lines = self.follow()
        elapsed = time.monotonic() - started
        self.assertEqual(self.followed(lines), ["flutter: mine"])
        self.assertEqual([line["kind"] for line in lines], ["line", "eof"], "no error line")
        bound = getattr(logs, "SESSION_PROBE_TIMEOUT", 5.0) + 2 + 10  # + --max-seconds + slack
        self.assertLess(elapsed, bound, f"the follow waited {elapsed:.1f}s on the simulator")

    def test_a_recorded_install_on_disk_asks_no_device(self) -> None:
        info_plist(self.bundle, BUNDLE)
        self.current_session(log_stream_executable="Runner",
                             log_stream_image_uuids=[APP_ARM64],
                             log_stream_bundle_path=str(self.bundle))
        self.write_state(simctl_hang={"simctl appinfo": 60})
        self.assert_only_mine(self.follow())
        self.assertFalse([argv for argv in self.calls() if "appinfo" in argv])

    def test_a_recorded_install_reused_by_another_app_is_not_trusted(self) -> None:
        info_plist(self.other, OTHER_BUNDLE)
        record = self.current_session(log_stream_executable="Runner",
                                      log_stream_image_uuids=[APP_ARM64],
                                      log_stream_bundle_path=str(self.other))
        self.assert_only_mine(self.follow())
        session.stop_session(reap=False)
        lines = self.follow("--session-id", record["session_id"])
        self.assertEqual(self.followed(lines), ["flutter: mine"])
        self.assertEqual(self.eof(lines)["image_uuids"], [APP_ARM64])

    def test_a_replay_asks_no_device(self) -> None:
        record = self.current_session(log_stream_executable="Runner",
                                      log_stream_image_uuids=[APP_ARM64])
        session.stop_session(reap=False)
        self.write_state(simctl_hang={"simctl appinfo": 60})
        for argv in (("--session-id", record["session_id"]),
                     ("--source", "device", "--session-id", record["session_id"])):
            with self.subTest(argv=argv):
                self.assertEqual(self.followed(self.follow(*argv)), ["flutter: mine"])
        self.assertFalse([argv for argv in self.calls() if "appinfo" in argv])

    def test_an_unrelated_path_is_followed_raw(self) -> None:
        record = self.current_session()
        other = Path(record["artifacts_dir"]) / "logs" / "copy.ndjson"
        other.write_text(f"{self.theirs}\n{self.mine}\n", encoding="utf-8")
        lines = self.follow("--path", "logs/copy.ndjson")
        self.assertEqual(self.followed(lines), ["flutter: theirs", "flutter: mine"])
        self.assertNotIn("image_uuids", self.eof(lines))

    def test_a_past_session_is_read_by_its_recorded_identity(self) -> None:
        record = self.current_session(log_stream_executable="Runner",
                                      log_stream_image_uuids=[APP_ARM64])
        session.stop_session(reap=False)
        shutil.rmtree(self.bundle.parent)  # the app is gone too
        self.write_state(app_info={}, app_bundle="(null)", installed=[])
        lines = self.follow("--session-id", record["session_id"])
        self.assertEqual(self.followed(lines), ["flutter: mine"])
        self.assertEqual(self.eof(lines)["image_uuids"], [APP_ARM64])

    def test_a_current_session_whose_app_is_gone_uses_its_recorded_identity(self) -> None:
        self.current_session(log_stream_executable="Runner",
                             log_stream_image_uuids=[APP_ARM64])
        shutil.rmtree(self.bundle.parent)
        self.write_state(app_info={}, app_bundle="(null)", installed=[])
        lines = self.follow()
        self.assertEqual(self.followed(lines), ["flutter: mine"])
        self.assertEqual(self.eof(lines)["image_uuids"], [APP_ARM64])
        code, tail = self.cli("logs", "tail")
        self.assertEqual(code, 0, tail)
        self.assertEqual(self.messages(tail["lines"]), ["flutter: mine"])

    def test_package_less_tail_reads_the_stream_as_the_sessions_app(self) -> None:
        self.current_session()
        code, tail = self.cli("logs", "tail")
        self.assertEqual(code, 0, tail)
        self.assertEqual(self.messages(tail["lines"]), ["flutter: mine"])
        self.assertEqual(tail["image_uuids"], sorted({APP_X86_64, APP_ARM64}))
        self.assertEqual(tail["executable"], "Runner")

    def test_device_follow_without_a_package_reads_the_stream_as_the_sessions_app(self) -> None:
        self.current_session(log_stream_pid=self.sleeper().pid)
        self.assert_only_mine(self.follow("--source", "device"))

    def test_replay_without_a_package_reads_it_as_the_sessions_app(self) -> None:
        record = self.current_session(log_stream_executable="Runner",
                                      log_stream_image_uuids=[APP_ARM64])
        session.stop_session(reap=False)
        lines = self.follow("--source", "device", "--session-id", record["session_id"])
        self.assertEqual(self.followed(lines), ["flutter: mine"])
        self.assertEqual(self.eof(lines)["image_uuids"], [APP_ARM64])

    def test_nothing_identifying_the_app_reads_raw(self) -> None:
        """A past session that recorded no identity: nothing to narrow by, so
        nothing is dropped (a filter with nothing to match would drop all)."""
        record = self.current_session()
        session.stop_session(reap=False)
        lines = self.follow("--session-id", record["session_id"])
        self.assertEqual(self.followed(lines), ["flutter: theirs", "flutter: mine"])


# --- 5. ps under a non-ASCII home, and round-1 registry rows ----------------------------


class _Processes(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.set_env(AUTONOM_HOME=str(self.root / "home"))

    def spawn(self, *argv: str) -> subprocess.Popen:
        child = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, child)
        self.assertTrue(_wait_until(lambda: processes.command_of(child.pid) is not None))
        return child

    def writer(self, destination: Path) -> subprocess.Popen:
        """The real bounded writer (`ios_simctl.spawn_bounded`) over a child
        that waits."""
        writer = ios_simctl.spawn_bounded(
            [sys.executable, "-c", "import time; time.sleep(120)"], destination)
        self.addCleanup(self._stop, writer)
        mark = logs._writer_mark()  # noqa: SLF001
        self.assertTrue(_wait_until(lambda: mark in (processes.command_of(writer.pid) or "")))
        return writer

    @staticmethod
    def _stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

    def record(self, directory: Path, session_id: str = "s_uuidtest") -> dict[str, Any]:
        directory.mkdir(parents=True, exist_ok=True)
        return {"session_id": session_id, "artifacts_dir": str(directory), "platform": "ios",
                "target_id": "FAKE-UDID-THAT-NOTHING-SERVES", "background": {}}

    def register(self, pid: int, record: dict[str, Any], signature: Any) -> None:
        processes.register("log_stream", pid, owner=record["session_id"],
                           session_id=record["session_id"],
                           artifacts_dir=record["artifacts_dir"], signature=signature)

    def rows(self) -> list[int]:
        return [row["pid"] for row in processes.entries()]


class NonAsciiPsTests(_Processes):
    def test_command_of_renders_non_ascii_arguments_under_lc_all_c(self) -> None:
        """Before: `LC_ALL=C` ps printed `cafM-CM-)-...`."""
        marker = f"{CAFE}-marker-{os.getpid()}"
        child = self.spawn(sys.executable, "-c", "import time; time.sleep(120)", marker)
        self.set_env(LC_ALL="C", LANG="C", LC_CTYPE="C")
        self.assertIn(marker, processes.command_of(child.pid) or "")
        found = dict(processes._running_processes())  # noqa: SLF001
        self.assertIn(marker, found.get(child.pid, ""))

    def test_a_writer_under_a_non_ascii_home_is_ours_and_is_stopped(self) -> None:
        """Before: read as `pid_reused`, left running, and its registry row
        dropped — so `cleanup --all` lost it too."""
        record = self.record(self.root / f"{CAFE}-home" / "sessions" / "s_uuidtest")
        destination = logs.stream_destination(record)
        writer = self.writer(destination)
        self.register(writer.pid, record, logs.writer_signature(destination))
        self.set_env(LC_ALL="C", LANG="C", LC_CTYPE="C")
        self.assertEqual(logs.log_writer_state(record, writer.pid), "ours")
        self.assertEqual(logs.stop_log_writer(record, writer.pid),
                         {"pid": writer.pid, "result": "terminated"})
        self.assertTrue(_wait_until(lambda: writer.poll() is not None))
        self.assertNotIn(writer.pid, self.rows())


class StopLogWriterRowTests(_Processes):
    """When `stop_log_writer` may drop a registry row."""

    def test_a_row_whose_signature_still_matches_is_kept(self) -> None:
        """The pid is not this session's writer, but it is the writer the row
        describes: neither signalled nor forgotten."""
        record = self.record(self.root / "sessions" / "s_uuidtest")
        elsewhere = self.root / "elsewhere" / "logs" / "stream.ndjson"
        writer = self.writer(elsewhere)
        self.register(writer.pid, record, logs.writer_signature(elsewhere))
        self.assertEqual(logs.stop_log_writer(record, writer.pid)["result"], "pid_reused")
        self.assertIsNone(writer.poll())
        self.assertIn(writer.pid, self.rows(), "the row still tracks a live writer")

    def test_a_row_proven_to_be_someone_elses_is_dropped(self) -> None:
        record = self.record(self.root / "sessions" / "s_uuidtest")
        stranger = self.spawn(sys.executable, "-c", "import time; time.sleep(120)")
        self.register(stranger.pid, record, logs.writer_signature(logs.stream_destination(record)))
        self.assertEqual(logs.stop_log_writer(record, stranger.pid)["result"], "pid_reused")
        self.assertIsNone(stranger.poll())
        self.assertNotIn(stranger.pid, self.rows())

    def test_a_path_ps_did_not_render_is_unverified_and_the_row_kept(self) -> None:
        """A writer whose non-ASCII path `ps` shows as escapes cannot be told
        from another writer: nothing is signalled, nothing forgotten."""
        record = self.record(self.root / f"{CAFE}-home" / "sessions" / "s_uuidtest")
        destination = logs.stream_destination(record)
        writer = self.writer(destination)
        self.register(writer.pid, record, logs.writer_signature(destination))
        rendered = processes.command_of(writer.pid).replace(CAFE, "cafM-CM-)")
        with mock.patch.object(processes, "command_of", return_value=rendered):
            self.assertEqual(logs.log_writer_state(record, writer.pid), "unverified")
            self.assertEqual(logs.stop_log_writer(record, writer.pid)["result"],
                             "unverified_skipped")
        self.assertIsNone(writer.poll())
        self.assertIn(writer.pid, self.rows())

    def test_a_gone_writers_row_is_dropped(self) -> None:
        record = self.record(self.root / "sessions" / "s_uuidtest")
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=30)
        self.register(child.pid, record, logs.writer_signature(logs.stream_destination(record)))
        self.assertEqual(logs.stop_log_writer(record, child.pid)["result"], "already_exited")
        self.assertNotIn(child.pid, self.rows())


class RoundOneRowTests(_Processes):
    """A log-stream row whose signature is the plain stream path."""

    def test_a_tail_of_the_stream_is_never_signalled(self) -> None:
        """Before: `tail -f <stream>` carries the path, so it was killed."""
        record = self.record(self.root / "sessions" / "s_uuidtest")
        stream = logs.stream_destination(record)
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text("", encoding="utf-8")
        tail = self.spawn("tail", "-f", str(stream))
        self.register(tail.pid, record, str(stream))
        (row,) = processes.entries()
        self.assertEqual(processes.terminate_entry(row), "pid_reused")
        self.assertIsNone(tail.poll(), "a tail of the stream was signalled")
        self.assertFalse(processes.entry_matches(row, processes.command_of(tail.pid)))
        result = processes.reap_session(record)
        self.assertEqual(result["terminated"], [{"kind": "log_stream", "pid": tail.pid,
                                                 "result": "pid_reused"}])
        self.register(tail.pid, record, str(stream))
        with mock.patch.object(processes, "discover_proxies", return_value=[]):
            outcome = processes.cleanup(include_live=True)
        self.assertEqual([(a["pid"], a["result"]) for a in outcome["actions"]],
                         [(tail.pid, "pid_reused")])
        time.sleep(0.3)
        self.assertIsNone(tail.poll(), "a tail of the stream was signalled")

    def test_the_real_writer_is_still_stopped(self) -> None:
        record = self.record(self.root / "sessions" / "s_uuidtest")
        stream = logs.stream_destination(record)
        writer = self.writer(stream)
        self.register(writer.pid, record, str(stream))
        result = processes.reap_session(record)
        self.assertEqual(result["terminated"], [{"kind": "log_stream", "pid": writer.pid,
                                                 "result": "terminated"}])
        self.assertTrue(_wait_until(lambda: writer.poll() is not None))

    def test_other_rows_keep_the_plain_substring_rule(self) -> None:
        self.assertTrue(processes.entry_matches(
            {"kind": "canvas_child", "signature": "bridge.mjs"}, "node /x/bridge.mjs --port 1"))
        self.assertTrue(processes.entry_matches(
            {"kind": "idb_companion", "signature": ["idb_companion", UDID]},
            f"/x/idb_companion --udid {UDID}"))
        self.assertFalse(processes.entry_matches(
            {"kind": "log_stream", "signature": "/s/logs/stream.ndjson"},
            "tail -f /s/logs/stream.ndjson"))


if __name__ == "__main__":
    unittest.main()
