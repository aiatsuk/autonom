"""Screenshots have to survive leaving the terminal.

Two properties matter more than the pixels:

1. A shot taken while a mock was active must say so. Fabricated data looks
   exactly like real data in an image, and an unlabelled screenshot of a mocked
   response is a convincing lie waiting to be pasted into a ticket.
2. The annotation must travel *inside* the file, because the index stays behind
   the moment someone drags the PNG somewhere else.

The previous behaviour lost both: no session copy when `--out` was given, and a
single `shots/latest.png` overwritten on every capture, so a run of twenty
screenshots left exactly one.
"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import screenshot as shot  # noqa: E402

def _segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _jpeg(width: int, height: int, *, sof: int = 0xC0, before: bytes = b"") -> bytes:
    """A JPEG of the given size: SOI, an APP0 (JFIF), whatever ``before``
    adds, then the start-of-frame segment ``sof`` and EOI."""
    app0 = _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    frame = _segment(sof, b"\x08" + struct.pack(">HH", height, width)
                     + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + before + frame + b"\xff\xd9"


# Smallest valid PNG: signature, IHDR, IDAT, IEND.
def _minimal_png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")
    return (shot.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


class NamingTests(unittest.TestCase):
    def test_labels_stay_legible_including_unicode(self) -> None:
        self.assertEqual(shot.slugify("Programación con datos"), "programación-con-datos")
        self.assertEqual(shot.slugify("  Two   words!! "), "two-words")

    def test_an_empty_label_still_produces_a_usable_name(self) -> None:
        self.assertEqual(shot.slugify(None), "shot")
        self.assertEqual(shot.slugify("!!!"), "shot")

    def test_sequence_makes_a_listing_sort_in_capture_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(shot.next_index(directory), 1)
            (directory / "0001_101010_first.png").write_bytes(b"")
            (directory / "0007_101011_seventh.png").write_bytes(b"")
            self.assertEqual(shot.next_index(directory), 8)

    def test_jpeg_shots_count_in_the_sequence(self) -> None:
        """An agent JPEG and a PNG never get the same number (AS-03)."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "0001_101010_first.png").write_bytes(b"")
            (directory / "0004_101011_agent.jpg").write_bytes(b"")
            (directory / "0009_101012_note.txt").write_bytes(b"")
            self.assertEqual(shot.next_index(directory), 5)

    def test_a_jpeg_name_ends_in_jpg(self) -> None:
        name = shot.build_filename(3, "home", moment=1_000_000, suffix=".jpg")
        self.assertRegex(name, r"^0003_\d{6}_home\.jpg$")
        self.assertTrue(shot.build_filename(3, "home", moment=1_000_000).endswith(".png"))

    def test_filenames_never_collide_within_a_second(self) -> None:
        """A tight loop used to overwrite: `latest.png` every time."""
        first = shot.build_filename(1, "same", moment=1_000_000)
        second = shot.build_filename(2, "same", moment=1_000_000)
        self.assertNotEqual(first, second)


class PngSizeTests(unittest.TestCase):
    def test_size_comes_from_the_header_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "shot.png"
            png.write_bytes(_minimal_png())
            self.assertEqual(shot.png_size(png), (1, 1))

    def test_anything_that_is_not_a_png_reads_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "notes.txt"
            other.write_bytes(b"not a png at all, but long enough to read 24 bytes")
            self.assertIsNone(shot.png_size(other))
            truncated = Path(tmp) / "short.png"
            truncated.write_bytes(shot.PNG_SIGNATURE + b"\x00\x00")
            self.assertIsNone(shot.png_size(truncated))
            self.assertIsNone(shot.png_size(Path(tmp) / "missing.png"))


class JpegSizeTests(unittest.TestCase):
    """RISK-013: an agent JPEG's size comes from its start-of-frame segment."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, data: bytes, name: str = "shot.jpg") -> Path:
        path = Path(self.tmp.name) / name
        path.write_bytes(data)
        return path

    def test_baseline_and_progressive_frames(self) -> None:
        self.assertEqual(shot.jpeg_size(self.write(_jpeg(450, 1000))), (450, 1000))
        self.assertEqual(shot.jpeg_size(self.write(_jpeg(1568, 706, sof=0xC2))), (1568, 706))

    def test_segments_and_fill_bytes_before_the_frame_are_skipped(self) -> None:
        tables = (_segment(0xDB, bytes(65)) + _segment(0xC4, bytes(30))
                  + _segment(0xE1, b"Exif\x00\x00" + bytes(40)) + b"\xff\xff")
        self.assertEqual(shot.jpeg_size(self.write(_jpeg(720, 1280, before=tables))),
                         (720, 1280))

    def test_a_huffman_table_is_not_a_frame(self) -> None:
        """0xC4 sits in the SOF range: reading it as a frame would give a
        wrong size; with no frame after it there is no size."""
        data = b"\xff\xd8" + _segment(0xC4, bytes(30)) + b"\xff\xd9"
        self.assertIsNone(shot.jpeg_size(self.write(data)))

    def test_anything_without_a_readable_frame_is_unknown(self) -> None:
        for data in (b"\xff\xd8\xff\xd9",                      # no frame
                     b"\xff\xd8" + _segment(0xE0, bytes(10))[:5],  # cut short
                     b"\xff\xd8\x00\x01",                        # no marker
                     _jpeg(0, 100),                                 # no width
                     b"\xff\xd8\xff\xda\x00\x08",                # scan before a frame
                     b"\xff\xd8\xff\xe0\x00\x01"):               # impossible length
            with self.subTest(data=data[:12]):
                self.assertIsNone(shot.jpeg_size(self.write(data)))
        self.assertIsNone(shot.jpeg_size(self.write(_minimal_png(), "shot.png")))
        self.assertIsNone(shot.jpeg_size(Path(self.tmp.name) / "missing.jpg"))

    def test_image_size_reads_either_format(self) -> None:
        self.assertEqual(shot.image_size(self.write(_minimal_png(), "a.png")), (1, 1))
        self.assertEqual(shot.image_size(self.write(_jpeg(450, 1000))), (450, 1000))
        self.assertIsNone(shot.image_size(self.write(b"plain text", "a.txt")))

    def test_a_jpeg_has_no_png_metadata(self) -> None:
        """The PNG-only iTXt reader skips a JPEG instead of walking its bytes
        as PNG chunks, and embedding declines it untouched."""
        path = self.write(_jpeg(450, 1000))
        before = path.read_bytes()
        self.assertEqual(shot.read_metadata(path), {})
        self.assertFalse(shot.embed_metadata(path, {"label": "x"}))
        self.assertEqual(path.read_bytes(), before)


class AgentCaptureTests(unittest.TestCase):
    """`capture_evidence(agent=True)` with the helper's JPEG, and its PNG
    fall-back; the helper itself is replaced here (see
    tests/test_agent_screenshot.py for the CLI through the fake helper)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = {"artifacts_dir": self.tmp.name, "session_id": "s_test",
                        "app_id": "com.example.app"}
        from autonom_lib.platform import Target

        self.target = Target("android", "emulator-5612", "adb", {"serial": "emulator-5612"})
        for name, value in (("foreground", None),
                            ("active_mocks", {"active": 0, "ids": []}),
                            ("pretest_attribution", {})):
            patcher = mock.patch.object(shot, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def helper(self, result: tuple):
        from autonom_lib import ui_android

        return mock.patch.object(ui_android, "helper_screenshot", return_value=result)

    def test_the_jpeg_is_filed_with_its_scale(self) -> None:
        jpeg = {"data": _jpeg(450, 1000), "width": 450, "height": 1000, "screen": (1080, 2400)}
        with self.helper((jpeg, None, None)) as asked, \
                mock.patch.object(shot, "capture_target") as screencap:
            result = shot.capture_evidence(self.target, self.session, label="home", agent=True,
                                           max_edge=1000)
        asked.assert_called_once_with(self.target, 1000)
        screencap.assert_not_called()
        path = Path(result["path"])
        self.assertRegex(path.name, r"^0001_\d{6}_home\.jpg$")
        self.assertEqual(path.read_bytes(), jpeg["data"])
        self.assertEqual((result["format"], result["width"], result["height"],
                          result["screen"], result["scale"]),
                         ("jpeg", 450, 1000, [1080, 2400], 2.4))
        self.assertEqual(result["coordinates"],
                         "image pixel (x, y) → ui tap --x round(x*2.4) --y round(y*2.4)")
        self.assertIs(result["metadata_embedded"], False)
        row = shot.load_index(self.session)[-1]
        self.assertEqual((row["format"], row["screen"], row["scale"]),
                         ("jpeg", [1080, 2400], 2.4))
        self.assertEqual(row["label"], "home")

    def test_without_the_helper_the_png_says_why(self) -> None:
        def screencap(_target, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_minimal_png())
            return destination

        with self.helper((None, "javac not found", "Install a JDK.")), \
                mock.patch.object(shot, "capture_target", side_effect=screencap):
            result = shot.capture_evidence(self.target, self.session, agent=True)
        self.assertTrue(result["path"].endswith(".png"))
        self.assertEqual((result["format"], result["screen"], result["scale"]),
                         ("png", [1, 1], 1))
        self.assertEqual((result["fallback_reason"], result["fallback_hint"]),
                         ("javac not found", "Install a JDK."))
        self.assertIs(result["metadata_embedded"], True)
        row = shot.load_index(self.session)[-1]
        self.assertEqual(set(row), {"captured_at", "label", "task", "session_id", "platform",
                                    "target_id", "app_id", "foreground", "mocks_active",
                                    "file", "width", "height"},
                         "a PNG's row gains no key")

    def test_a_png_without_agent_is_unchanged(self) -> None:
        def screencap(_target, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_minimal_png())
            return destination

        with self.helper((None, "x", None)) as asked, \
                mock.patch.object(shot, "capture_target", side_effect=screencap):
            result = shot.capture_evidence(self.target, self.session, label="plain")
        asked.assert_not_called()
        self.assertEqual(set(result), {"path", "copies", "metadata", "metadata_embedded",
                                       "width", "height"})

    def test_a_mocked_jpeg_says_where_the_fact_is(self) -> None:
        jpeg = {"data": _jpeg(450, 1000), "width": 450, "height": 1000, "screen": (1080, 2400)}
        with self.helper((jpeg, None, None)), \
                mock.patch.object(shot, "active_mocks", return_value={"active": 1, "ids": ["m"]}):
            result = shot.capture_evidence(self.target, self.session, agent=True)
        warning = result["warnings"][0]
        self.assertEqual(warning["code"], "screenshot_shows_mocked_data")
        self.assertIn("shots index", warning["hint"])
        self.assertEqual(shot.load_index(self.session)[-1]["mocks_active"], 1)


class EmbeddedMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.png = Path(self.tmp.name) / "shot.png"
        self.png.write_bytes(_minimal_png())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_metadata_round_trips_through_the_file(self) -> None:
        fields = {"label": "Programación con datos", "mocks_active": 1, "mocks": "m_1"}
        self.assertTrue(shot.embed_metadata(self.png, fields))
        read = shot.read_metadata(self.png)
        self.assertEqual(read["label"], "Programación con datos")
        self.assertEqual(read["mocks_active"], "1")
        self.assertEqual(read["mocks"], "m_1")

    def test_the_png_stays_a_valid_png(self) -> None:
        """Annotation must not cost readability in every other tool."""
        before = self.png.read_bytes()
        shot.embed_metadata(self.png, {"label": "x"})
        after = self.png.read_bytes()
        self.assertTrue(after.startswith(shot.PNG_SIGNATURE))
        self.assertTrue(after.endswith(before[-12:]), "IEND must remain last")
        self.assertGreater(len(after), len(before))

    def test_chunk_crc_is_correct(self) -> None:
        """A bad CRC makes strict decoders reject the whole image."""
        shot.embed_metadata(self.png, {"label": "vérification"})
        data = self.png.read_bytes()
        offset = len(shot.PNG_SIGNATURE)
        checked = 0
        while offset + 8 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            body = data[offset + 4:offset + 8 + length]
            stored = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
            self.assertEqual(zlib.crc32(body) & 0xFFFFFFFF, stored)
            checked += 1
            if data[offset + 4:offset + 8] == b"IEND":
                break
            offset += 12 + length
        self.assertGreaterEqual(checked, 4)

    def test_a_non_png_is_declined_rather_than_corrupted(self) -> None:
        other = Path(self.tmp.name) / "notes.txt"
        other.write_bytes(b"not a png")
        self.assertFalse(shot.embed_metadata(other, {"label": "x"}))
        self.assertEqual(other.read_bytes(), b"not a png")

    def test_none_values_are_omitted_not_written_as_the_string_none(self) -> None:
        shot.embed_metadata(self.png, {"label": "x", "task": None})
        self.assertNotIn("task", shot.read_metadata(self.png))


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.session = {"artifacts_dir": self.tmp.name, "session_id": "s_test"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_shots_group_under_a_task_directory(self) -> None:
        plain = shot.shots_dir(self.session)
        grouped = shot.shots_dir(self.session, "Réponses simulées")
        self.assertEqual(plain.name, "shots")
        self.assertEqual(grouped.parent, plain)
        self.assertEqual(grouped.name, "réponses-simulées")

    def test_index_appends_and_survives_a_torn_line(self) -> None:
        shot.append_index(self.session, {"file": "shots/0001_a.png", "label": "one"})
        shot.append_index(self.session, {"file": "shots/0002_b.png", "label": "two"})
        with open(shot.index_path(self.session), "a", encoding="utf-8") as handle:
            handle.write('{"file": "shots/0003')  # interrupted mid-write
        entries = shot.load_index(self.session)
        self.assertEqual([e["label"] for e in entries], ["one", "two"])

    def test_a_missing_index_reads_as_empty(self) -> None:
        self.assertEqual(shot.load_index(self.session), [])


if __name__ == "__main__":
    unittest.main()
