"""Owned-frame VideoToolbox tests: no emulator, listener or shared memory."""
import json
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "plugins/autonom/skills/android-emulator-browser/scripts/host-video.swift"

def frame(w=64, h=64, target=None, payload=None):
    target = target or (w, h)
    pixels = payload if payload is not None else bytes([200, 40, 70, 255]) * (w*h)
    return struct.pack(">8I", 0x41463031, len(pixels), w, h, w*4, 1, *target) + pixels

def packets(data):
    assert len(data) >= 68
    assert struct.unpack(">I", data[64:68])[0] == 0x68323634
    data = data[68:]
    result = []
    while data:
        assert len(data) >= 12
        flags, size = struct.unpack(">QI", data[:12])
        if flags >> 32 == 0x80000000:
            result.append(("size", (flags & 0xffffffff, size)))
            data = data[12:]
        else:
            assert len(data) >= 12+size
            result.append(("config" if flags & 1 << 62 else "key" if flags & 1 << 61 else "frame", data[12:12+size]))
            data = data[12+size:]
    return result

@unittest.skipUnless(sys.platform == "darwin", "VideoToolbox requires macOS")
class OwnedFramesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="autonom-owned-frames-")
        cls.binary = str(Path(cls.temp.name) / "encoder")
        subprocess.run(["xcrun", "swiftc", "-O", "-warnings-as-errors", str(SOURCE), "-o", cls.binary], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_helper(self, data):
        return subprocess.run([self.binary], input=data, capture_output=True, timeout=15)

    def test_owned_frame_hardware_and_config(self):
        result = self.run_helper(frame())
        self.assertEqual(result.returncode, 0, result.stderr)
        items = packets(result.stdout)
        self.assertEqual(items[0], ("size", (64, 64)))
        self.assertEqual(items[1][0], "config")
        self.assertEqual(items[2][0], "key")
        stats = json.loads(result.stderr)
        self.assertTrue(stats["hardwareRequired"])
        self.assertEqual(stats["encoded"], 1)
        self.assertLessEqual(stats["peakPending"], 2)

    def test_resize_rotation_crop_and_session_order(self):
        result = self.run_helper(frame() + frame(96, 64, (32, 64)) + frame(64, 96, (64, 32)))
        self.assertEqual(result.returncode, 0, result.stderr)
        items = packets(result.stdout)
        self.assertEqual([v for k,v in items if k == "size"], [(64,64),(32,64),(64,32)])
        self.assertEqual([k for k,_ in items], ["size","config","key"]*3)

    def test_idle_keyframe_uses_owned_last_pixel(self):
        key = struct.pack(">8I", 0x41463031,0,0,0,0,0,0,0)
        result = self.run_helper(frame() + key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(k=="key" for k,_ in packets(result.stdout)), 2)

    def test_reject_truncated_and_invalid_buffers(self):
        for data in (frame()[:-1], frame()[:10], frame(64,64,payload=b"x"),
                     struct.pack(">8I",0x41463031,0xffffffff,8192,8192,32768,1,64,64),
                     frame(64,64,(0,64))):
            with self.subTest(size=len(data)):
                result = self.run_helper(data)
                self.assertEqual(result.returncode, 2, result.stderr)

    @unittest.skipUnless(shutil.which("ffmpeg"), "independent H264 decode requires ffmpeg")
    def test_bottom_up_rgba_is_upright_and_not_bgr(self):
        # Bottom half in memory becomes the upper half of the displayed image.
        pixels = bytes([20,40,210,255])*(64*32) + bytes([200,40,70,255])*(64*32)
        data = bytearray(frame(payload=pixels))
        struct.pack_into(">I", data, 20, 17)
        result = self.run_helper(data)
        self.assertEqual(result.returncode,0,result.stderr)
        annexb = b"".join(v for k,v in packets(result.stdout) if k != "size")
        decoded = subprocess.run(["ffmpeg","-v","error","-f","h264","-i","pipe:0",
            "-frames:v","1","-f","rawvideo","-pix_fmt","rgb24","pipe:1"],
            input=annexb,capture_output=True,timeout=10)
        self.assertEqual(decoded.returncode,0,decoded.stderr)
        self.assertEqual(len(decoded.stdout),64*64*3)
        top = decoded.stdout[(16*64+32)*3:(16*64+32)*3+3]
        bottom = decoded.stdout[(48*64+32)*3:(48*64+32)*3+3]
        self.assertGreater(top[0],170);self.assertLess(top[2],100)
        self.assertLess(bottom[0],50);self.assertGreater(bottom[2],180)

    @unittest.skipUnless(shutil.which("ffmpeg"), "independent H264 decode requires ffmpeg")
    def test_android_rotation_uses_owned_rotated_pixels(self):
        pixels = bytes([200,40,70,255])*(64*48) + bytes([20,40,210,255])*(64*48)
        data = bytearray(frame(64,96,(96,64),pixels))
        struct.pack_into(">I",data,20,257)
        result = self.run_helper(data)
        self.assertEqual(result.returncode,0,result.stderr)
        items=packets(result.stdout)
        self.assertEqual(items[0],("size",(96,64)))
        decoded=subprocess.run(["ffmpeg","-v","error","-f","h264","-i","pipe:0","-frames:v","1","-f","rawvideo","-pix_fmt","rgb24","pipe:1"],input=b"".join(v for k,v in items if k!="size"),capture_output=True,timeout=10)
        self.assertEqual(decoded.returncode,0,decoded.stderr)
        left=decoded.stdout[(32*96+24)*3:(32*96+24)*3+3]
        right=decoded.stdout[(32*96+72)*3:(32*96+72)*3+3]
        self.assertGreater(left[0],170);self.assertGreater(right[2],180)

    def test_empty_input_exits(self):
        result = self.run_helper(b"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stderr)["encoded"],0)

if __name__ == "__main__":
    unittest.main()
