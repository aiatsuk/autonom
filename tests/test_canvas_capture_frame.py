"""Framed exports exercise the real encoder without a device or user's gallery."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from autonom_lib import canvas_capture_frame as frame
from autonom_lib import errors
from autonom_lib.device_frames import FrameRepository
from types import SimpleNamespace


class FrameValidationTests(unittest.TestCase):
    def test_disabled_frame_needs_no_tools_and_does_not_touch_the_file(self):
        with mock.patch.object(frame.shutil, "which", return_value=None):
            self.assertIsNone(frame.options(None))
            frame.render(Path("nonexistent"), None)
            with self.assertRaises(errors.AutonomError):
                frame.options({"corner": .12, "inset": .02})

    def test_untrusted_geometry_is_refused(self):
        for value in ({}, "native", {"corner": .12, "inset": float("nan")},
                      {"corner": .12, "inset": .2}, {"corner": True, "inset": .02},
                      {"corner": .12, "inset": .02, "path": "/tmp/x"}):
            with self.subTest(value=value), self.assertRaises(errors.AutonomError):
                frame.options(value)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class FrameExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repository = FrameRepository(SimpleNamespace(platform="ios", target_id="fake", tool="fake"), self.root/"cache")
        self.repository.source = {"source":"generic","model":"Unknown","fallbackReason":"test"}
        self.geometry = self.repository.snapshot({"mode":"auto","rotation":0})

    def probe(self, path):
        return json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))["streams"][0]

    def pixels(self, path):
        return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"])

    def test_unavailable_png_frame_preserves_original_bytes(self):
        for width,height in ((240,520),(520,240)):
            path=self.root/"shot.png"
            frame._png(path,width,height,[bytes((22,123,231,255))*width]*height)
            before=path.read_bytes()
            frame.render(path,self.geometry,repository=self.repository)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(self.root.glob(".frame-*")),[])

    def test_unavailable_video_frame_preserves_original_bytes(self):
        path=self.root/"video.mp4"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",
                        "testsrc2=size=240x520:rate=60:duration=0.5","-c:v","libx264",str(path)],check=True)
        before=path.read_bytes()
        frame.render(path,self.geometry,video=True,repository=self.repository)
        self.assertEqual(path.read_bytes(),before)

    def test_failed_render_preserves_original(self):
        path = self.root / "invalid.mp4"
        path.write_bytes(b"original")
        with self.assertRaises(errors.AutonomError):
            frame.render(path, self.geometry, video=True, repository=self.repository)
        self.assertEqual(path.read_bytes(), b"original")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class RecordingTimelineTests(unittest.TestCase):
    def test_idle_tail_keeps_wall_duration_without_speeding_up_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"video.mp4"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",
                            "testsrc2=size=120x200:rate=10:duration=0.5",
                            "-c:v","libx264",str(path)],check=True)
            frame.render(path,None,video=True,duration_s=1.5)
            stream=json.loads(subprocess.check_output(["ffprobe","-v","error","-show_streams",
                                                      "-of","json",str(path)]))["streams"][0]
            self.assertAlmostEqual(float(stream["duration"]),1.5,places=2)
            self.assertEqual(int(stream["nb_frames"]),45)
            self.assertAlmostEqual(frame.video_duration(path),1.5,places=2)
