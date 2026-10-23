"""Synthetic original-frame fixtures: no vendor artwork is stored in the repo."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import shutil

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from autonom_lib import device_frames as frames
from autonom_lib.canvas_capture_frame import _png

LAYOUT = """parts {
device { display { width 40 height 80 x 0 y 0 } }
portrait { background { image back.png } foreground { mask mask.png cutout hole } }
}
layouts { portrait { width 50 height 100 part1 { name portrait x 0 y 0 } part2 { name device x 4 y 8 } } }
"""

class GeometryTests(unittest.TestCase):
    def test_rotations_preserve_screen_and_asymmetric_insets(self):
        b=dict(width=50,height=100);s=dict(x=4,y=8,width=40,height=80)
        expected={0:(50,100,4,8,40,80),90:(100,50,12,4,80,40),
                  180:(50,100,6,12,40,80),270:(100,50,8,6,80,40)}
        for a,e in expected.items():
            body,rect=frames.oriented_rect(b,s,a)
            self.assertEqual((*body.values(),*rect.values()),e)

    def test_android_orientation_uses_primary_display_and_correct_direction(self):
        target=SimpleNamespace(tool="adb",target_id="fixture")
        for value,expected in ((0,0),(1,270),(2,180),(3,90),(90,270),(270,90)):
            text=f"Display: mDisplayId=33\n  mRotation=ROTATION_180\nDisplay: mDisplayId=0\n  mRotation=ROTATION_{value}\n"
            with patch.object(frames,"command",return_value=text.encode()):
                self.assertEqual(frames.android_rotation(target),expected)

    def test_invalid_public_geometry_is_an_input_error(self):
        repo=frames.FrameRepository(None)
        for payload in ({},{"width":True,"height":20},{"width":20,"height":20,"rotation":45}):
            with self.assertRaises(frames.errors.AutonomError) as error:
                repo.describe(payload)
            self.assertEqual(error.exception.code,frames.errors.INVALID_VALUE)

    def test_parser_rejects_broken_layout_and_path_escape(self):
        for text in ("a { b c", "a b a c", "}"):
            with self.assertRaises(ValueError):
                frames.parse_layout(text)
        with self.assertRaises(ValueError):
            frames.asset(Path("/tmp/skin"), "../secret.png")

    def test_incompatible_aspect_and_unknown_model_are_honest(self):
        repo=frames.FrameRepository(SimpleNamespace(platform="android",tool="missing",target_id="physical"))
        repo.source=dict(id="abc",model="Test Phone",source="android-sdk-skin",
                         bodySize=dict(width=50,height=100),screenRect=dict(x=4,y=8,width=40,height=80))
        result=repo.resolve(80,80)
        self.assertEqual(result["source"],"generic")
        self.assertIn("proportions",result["fallbackReason"])
        self.assertEqual(result["layers"],[])
        repo.source=dict(model="iPhone 17 Pro",source="generic",fallbackReason="Original full body unavailable")
        self.assertEqual(repo.resolve(1206,2622)["model"],"iPhone 17 Pro")

    def test_selected_model_does_not_reuse_avd_frame(self):
        repo=frames.FrameRepository(SimpleNamespace(platform="android",tool="adb",target_id="fixture"))
        repo.source=dict(model="Pixel 6 Pro",source="generic",fallbackReason="fixture")
        with patch.object(frames,"android_source",side_effect=ValueError("missing")) as adapter:
            selected=repo.discover("pixel-10")
            self.assertEqual(selected["model"],"Pixel 10")
            adapter.assert_called_once_with(repo.target,model_override="pixel_10")
            snap=repo.snapshot(dict(mode="auto",rotation=0,preset="pixel-10"))
            self.assertEqual(snap["source"]["model"],"Pixel 10")
            self.assertEqual(repo.discover()["model"],"Pixel 6 Pro")
        for preset in ("../private",[],{}):
            with self.assertRaises(frames.errors.AutonomError):
                repo.snapshot(dict(mode="auto",rotation=0,preset=preset))

    def test_snapshot_rejects_client_paths_and_survives_model_changes(self):
        repo=frames.FrameRepository(None);repo.source=dict(model="A",source="generic",fallbackReason="test")
        snap=repo.snapshot(dict(mode="auto",rotation=90))
        repo.source["model"]="B"
        self.assertEqual(repo.resolve(400,800,snapshot=snap)["model"],"A")
        for v in ({"mode":"auto","rotation":True},{"mode":"auto","rotation":45},
                  {"mode":"auto","rotation":0,"path":"/private/key"},{}):
            with self.assertRaises(Exception):
                repo.snapshot(v)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"ffmpeg required")
class SkinTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.skin=self.root/"skins"/"fixture";self.skin.mkdir(parents=True)
        (self.skin/"layout").write_text(LAYOUT)
        body=[bytearray(bytes((30,40,50,255))*50) for _ in range(100)]
        for row in body[8:88]:
            row[4*4:44*4]=b"\0\0\0\0"*40
        _png(self.skin/"back.png",50,100,body)
        # Transparent display and one opaque camera pixel at an asymmetric position.
        rows=[bytearray(b"\0\0\0\0"*40) for _ in range(80)]
        rows[3][5*4:6*4]=bytes((0,0,0,255))
        _png(self.skin/"mask.png",40,80,rows)
        self.avd=self.root/"fixture.avd";self.avd.mkdir()
        (self.avd/"config.ini").write_text("hw.device.name=fixture\nskin.path="+str(self.skin))
        self.target=SimpleNamespace(platform="android",tool=str(self.root/"platform-tools/adb"),target_id="emulator-5580")

    def test_small_original_rectangular_skin_clears_display_without_an_occlusion_mask(self):
        (self.skin/"layout").write_text(LAYOUT.replace("foreground { mask mask.png cutout hole }",
                                                       "onion { image glare.png }"))
        _png(self.skin/"back.png",50,100,[bytes((30,40,50,255))*50]*100)
        shutil.copytree(self.skin,self.root/"skins"/"nexus_5")
        repo=frames.FrameRepository(self.target,self.root/"cache")
        source=repo.discover("small")
        self.assertEqual(source["model"],"Small phone (Nexus 5)")
        desc=repo.resolve(40,80,snapshot={"source":source})
        self.assertEqual(desc["source"],"android-sdk-skin")
        import subprocess
        raw=subprocess.check_output(["ffmpeg","-v","error","-i",desc["layers"][0]["path"],
                                     "-frames:v","1","-f","rawvideo","-pix_fmt","rgba","-"])
        self.assertEqual(raw[(8*50+4)*4+3],0,"opaque original screen placeholder must be removed")
        self.assertEqual(raw[(7*50+4)*4+3],255,"original bezel must stay intact")
        mask=subprocess.check_output(["ffmpeg","-v","error","-i",desc["screenMask"]["path"],
                                      "-frames:v","1","-f","rawvideo","-pix_fmt","rgba","-"])
        self.assertTrue(all(value==255 for value in mask[3::4]))

    def test_explicit_model_uses_its_skin_instead_of_avd_skin(self):
        shutil.copytree(self.skin,self.root/"skins"/"pixel_10")
        with patch.object(frames.emulator,"running_avd_name",return_value="fixture"),patch.object(
                frames.emulator,"describe_avd",return_value={"path":str(self.avd)}),patch.object(
                frames,"android_rotation",return_value=0):
            repo=frames.FrameRepository(self.target,self.root/"cache")
            selected=repo.describe(dict(width=40,height=80,preset="pixel-10"))
            self.assertEqual(selected["model"],"Pixel 10")
            self.assertEqual(selected["source"],"android-sdk-skin")
            self.assertTrue(selected["layers"])
            self.assertEqual(repo.discover()["model"],"Fixture")
            snap=repo.snapshot(dict(mode="auto",rotation=0,preset="pixel-10"))
            self.assertEqual(repo.resolve(40,80,snapshot=snap)["model"],"Pixel 10")

    def test_artwork_padding_preserves_full_body_and_layout_screen(self):
        shutil.copytree(self.skin,self.root/"skins"/"pixel_10")
        back=self.root/"skins"/"pixel_10"/"back.png"
        _png(back,57,104,[bytes((30,40,50,255))*57]*104)
        source=frames.android_source(self.target,model_override="pixel_10")
        self.assertEqual(source["bodySize"],dict(width=57,height=104))
        self.assertEqual(source["screenRect"],dict(x=4,y=8,width=40,height=80))
        _png(back,49,100,[bytes((30,40,50,255))*49]*100)
        with self.assertRaisesRegex(ValueError,"dimensions"):
            frames.android_source(self.target,model_override="pixel_10")

    def test_explicit_model_does_not_query_unresponsive_avd(self):
        shutil.copytree(self.skin,self.root/"skins"/"pixel_10")
        with patch.object(frames.emulator,"running_avd_name",side_effect=AssertionError("Must use local artwork")):
            source=frames.android_source(self.target,model_override="pixel_10")
        self.assertEqual(source["model"],"Pixel 10")
        self.assertEqual(source["source"],"android-sdk-skin")

    def test_fold_states_select_separate_skins_and_landscape_layout(self):
        opened=self.root/"skins"/"pixel_fold"/"default"
        closed=self.root/"skins"/"pixel_fold"/"closed"
        shutil.copytree(self.skin,closed)
        opened.mkdir(parents=True)
        (opened/"layout").write_text(LAYOUT.replace("width 40 height 80","width 80 height 40")
            .replace("layouts { portrait { width 50 height 100","layouts { landscape { width 100 height 50")
            .replace("x 4 y 8","x 8 y 4"))
        _png(opened/"back.png",100,50,[bytes((30,40,50,255))*100]*50)
        _png(opened/"mask.png",80,40,[bytes((0,0,0,0))*80]*40)
        with patch.object(frames.emulator,"running_avd_name",return_value="fixture"),patch.object(
                frames.emulator,"describe_avd",return_value={"path":str(self.avd)}),patch.object(
                frames,"android_rotation",return_value=0):
            repo=frames.FrameRepository(self.target,self.root/"cache")
            for preset,size,label in (("pixel-fold",(80,40),"Pixel Fold (open)"),
                                      ("pixel-fold-closed",(40,80),"Pixel Fold (folded)")):
                desc=repo.describe(dict(width=size[0],height=size[1],preset=preset))
                self.assertEqual(desc["source"],"android-sdk-skin")
                self.assertEqual(desc["model"],label)
                self.assertEqual((desc["screenRect"]["width"],desc["screenRect"]["height"]),size)
                self.assertTrue(desc["layers"])
            snap=repo.snapshot(dict(mode="auto",rotation=0,preset="pixel-fold-closed"))
            repo.discover("pixel-fold")
            self.assertEqual(repo.resolve(40,80,snapshot=snap)["model"],"Pixel Fold (folded)")

    def test_adapter_cache_mask_and_public_descriptor(self):
        with patch.object(frames.emulator,"running_avd_name",return_value="fixture"),patch.object(
                frames.emulator,"describe_avd",return_value={"path":str(self.avd)}):
            repo=frames.FrameRepository(self.target,self.root/"cache")
            desc=repo.resolve(40,80)
            self.assertEqual(desc["screenRect"],dict(x=4,y=8,width=40,height=80))
            path=Path(desc["layers"][0]["path"]);mtime=path.stat().st_mtime_ns
            again=repo.resolve(80,160)
            self.assertEqual(path.stat().st_mtime_ns,mtime)
            self.assertEqual(again["id"],desc["id"])
            w,h,pixels=frames.rgba(Path(desc["screenMask"]["path"]))
            self.assertEqual(pixels[(3*w+5)*4+3],0)
            self.assertEqual(pixels[(20*w+20)*4+3],255)
            rotated=repo.resolve(80,40,90)
            self.assertEqual(rotated["screenRect"],dict(x=12,y=4,width=80,height=40))
            with patch.object(frames, "android_rotation", return_value=0):
                public=repo.describe(dict(width=40,height=80,rotation=0))
            self.assertNotIn(str(self.root),json.dumps(public))
            self.assertTrue(public["layers"][0]["image"].startswith("data:image/png;base64,"))
            # Source changes invalidate the normalized cache on a new discovery.
            (self.skin/"layout").write_text(LAYOUT+" # source version changed")
            next_repo=frames.FrameRepository(self.target,self.root/"cache")
            self.assertNotEqual(next_repo.resolve(40,80)["id"],desc["id"])

    def test_skin_export_preserves_exact_screen_pixels_and_camera_alignment(self):
        from autonom_lib.canvas_capture_frame import render
        with patch.object(frames.emulator,"running_avd_name",return_value="fixture"),patch.object(
                frames.emulator,"describe_avd",return_value={"path":str(self.avd)}):
            repo=frames.FrameRepository(self.target,self.root/"cache")
            for angle in (0,90,180,270):
                w,h=(40,80) if angle%180==0 else (80,40)
                shot=self.root/"shot.png";color=bytes((17,122,233,255))
                _png(shot,w,h,[color*w]*h)
                desc=repo.resolve(w,h,angle);screen=desc["screenRect"]
                render(shot,repo.snapshot(dict(mode="auto",rotation=angle)),repository=repo)
                ow,oh,pixels=frames.rgba(shot)
                self.assertEqual((ow,oh),tuple(desc["bodySize"].values()))
                x,y=screen["x"]+w//2,screen["y"]+h//2
                self.assertEqual(pixels[(y*ow+x)*4:(y*ow+x)*4+4],color)
                _,camera=frames.oriented_rect(dict(width=40,height=80),dict(x=5,y=3,width=1,height=1),angle)
                x,y=screen["x"]+camera["x"],screen["y"]+camera["y"]
                self.assertEqual(pixels[(y*ow+x)*4:(y*ow+x)*4+4],bytes((0,0,0,255)))

    def test_original_skin_video_preserves_frames_rate_and_duration(self):
        from autonom_lib.canvas_capture_frame import render
        import subprocess
        with patch.object(frames.emulator,"running_avd_name",return_value="fixture"),patch.object(
                frames.emulator,"describe_avd",return_value={"path":str(self.avd)}):
            repo=frames.FrameRepository(self.target,self.root/"cache")
            path=self.root/"capture.mp4"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",
                            "testsrc2=size=40x80:rate=60:duration=0.5","-c:v","libx264",str(path)],check=True)
            render(path,repo.snapshot(dict(mode="auto",rotation=0)),video=True,repository=repo)
            stream=json.loads(frames.command(["ffprobe","-v","error","-show_streams","-of","json",str(path)]))["streams"][0]
            self.assertEqual((stream["width"],stream["height"]),(50,100))
            self.assertEqual(stream["r_frame_rate"],"60/1")
            self.assertEqual(int(stream["nb_frames"]),30)
            self.assertAlmostEqual(float(stream["duration"]),0.5,places=2)
