"""Synthetic DeviceKit PDFs only; no Apple artwork is copied into tests."""
from pathlib import Path
import json
import plistlib
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from autonom_lib.device_frames import ios_chrome, FrameRepository, rgba
from autonom_lib.device_frame_pdf import PDFRenderer
from autonom_lib.canvas_capture_frame import _png, render


def pdf(path, width, height, content):
    stream = content.encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
               f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R /Resources << >> >>'.encode(),
               b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data = bytearray(b'%PDF-1.4\n'); offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref = len(data); data += b'xref\n0 5\n0000000000 65535 f \n'
    for offset in offsets:
        data += f'{offset:010d} 00000 n \n'.encode()
    data += f'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


class DeviceKitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile_path = self.root/'type/profile.plist'; self.profile_path.parent.mkdir()
        self.profile = dict(chromeIdentifier='test.chrome',mainScreenScale=3,mainScreenWidth=120,mainScreenHeight=240,framebufferMask='mask')
        self.profile_path.write_bytes(plistlib.dumps(self.profile))
        pdf(self.profile_path.parent/'mask.pdf',120,240,'0 0 120 240 re f')
        self.bundle = self.root/'Chrome/unrelated-filename.devicechrome/Contents'; self.bundle.mkdir(parents=True)
        (self.bundle/'Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier='test.chrome')))
        resources = self.bundle/'Resources'; resources.mkdir()
        names = ('topLeft','top','topRight','right','bottomRight','bottom','bottomLeft','left')
        for name in names:
            width,height = (1,10) if name in ('top','bottom') else (10,1) if name in ('left','right') else (10,10)
            pdf(resources/(name+'.pdf'),width,height,'0.2 0.2 0.2 rg -100 -100 1000 1000 re f')
        images = {name:name for name in names}
        images.update(sizing=dict(leftWidth=2,rightWidth=2,topHeight=2,bottomHeight=2),devicePadding=dict(left=1,right=1,top=0,bottom=0))
        (resources/'chrome.json').write_text(json.dumps(dict(identifier='test.chrome',images=images,inputs=[])))

    def source(self):
        return ios_chrome(self.profile_path,self.profile,[self.root/'Chrome'])

    def test_exact_identifier_and_profile_geometry_select_the_body(self):
        source = self.source()
        self.assertEqual(source['source'],'apple-devicekit')
        self.assertEqual(source['bodySize'],dict(width=138,height=252))
        self.assertEqual(source['screenRect'],dict(x=9,y=6,width=120,height=240))
        self.assertEqual(len(source['pdfLayers']),8)
        self.profile['chromeIdentifier']='missing'
        with self.assertRaises(ValueError):
            self.source()

    @unittest.skipUnless(sys.platform=='darwin' and shutil.which('ffmpeg'),'macOS CoreGraphics + ffmpeg required')
    def test_pdf_slices_are_clipped_and_visible_screen_is_not_overpainted(self):
        source = self.source(); source.update(id='synthetic-devicekit',model='Synthetic')
        repo = FrameRepository(None,self.root/'cache'); repo.source=source
        path = self.root/'shot.png'; color=bytes((17,122,233,255))
        _png(path,120,240,[color*120]*240)
        render(path,repo.snapshot(dict(mode='auto',rotation=0)),repository=repo)
        width,height,pixels = rgba(path)
        self.assertEqual((width,height),(138,252))
        # The PDF deliberately paints far outside its MediaBox. It must neither
        # overpaint the screen nor leak into the body's transparent side padding.
        self.assertEqual(pixels[:4],b'\0\0\0\0')
        for x,y in ((9,6),(128,6),(9,245),(128,245),(69,126)):
            self.assertEqual(pixels[(y*width+x)*4:(y*width+x)*4+4],color)
        self.assertEqual(pixels[(3*width+30)*4+3],255)

    @unittest.skipUnless(sys.platform=='darwin' and shutil.which('ffmpeg'),'macOS CoreGraphics + ffmpeg required')
    def test_pdf_placement_uses_top_left_coordinates(self):
        source = self.root/'red.pdf'; output=self.root/'placed.png'
        pdf(source,10,10,'1 0 0 rg -100 -100 1000 1000 re f')
        PDFRenderer().render(output,40,40,[(source,5,3,10,10)])
        width,_,pixels=rgba(output)
        def pixel(x,y):return pixels[(y*width+x)*4:(y*width+x)*4+4]
        self.assertEqual(pixel(5,3),bytes((255,0,0,255)))
        self.assertEqual(pixel(4,3),b'\0\0\0\0')
        self.assertEqual(pixel(5,13),b'\0\0\0\0')
