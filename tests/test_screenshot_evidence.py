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
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import screenshot as shot  # noqa: E402

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
    def test_labels_stay_legible_including_cyrillic(self) -> None:
        self.assertEqual(shot.slugify("Расписание с моком"), "расписание-с-моком")
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

    def test_filenames_never_collide_within_a_second(self) -> None:
        """A tight loop used to overwrite: `latest.png` every time."""
        first = shot.build_filename(1, "same", moment=1_000_000)
        second = shot.build_filename(2, "same", moment=1_000_000)
        self.assertNotEqual(first, second)


class EmbeddedMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.png = Path(self.tmp.name) / "shot.png"
        self.png.write_bytes(_minimal_png())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_metadata_round_trips_through_the_file(self) -> None:
        fields = {"label": "Расписание с моком", "mocks_active": 1, "mocks": "m_1"}
        self.assertTrue(shot.embed_metadata(self.png, fields))
        read = shot.read_metadata(self.png)
        self.assertEqual(read["label"], "Расписание с моком")
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
        shot.embed_metadata(self.png, {"label": "проверка"})
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
        grouped = shot.shots_dir(self.session, "Мок предзаказов")
        self.assertEqual(plain.name, "shots")
        self.assertEqual(grouped.parent, plain)
        self.assertEqual(grouped.name, "мок-предзаказов")

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
