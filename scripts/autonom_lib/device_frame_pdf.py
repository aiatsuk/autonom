"""Rasterize installed DeviceKit PDFs with macOS CoreGraphics (no extra packages).

This is an asset adapter, not a screenshot of Simulator or a new live renderer.
PDFs are read from the local developer installation and normalized once per cache.
"""
from __future__ import annotations
import ctypes as C
import math
from pathlib import Path
import sys


class Point(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class Size(C.Structure):
    _fields_ = [("width", C.c_double), ("height", C.c_double)]


class Rect(C.Structure):
    _fields_ = [("origin", Point), ("size", Size)]


class PDFRenderer:
    def __init__(self):
        if sys.platform != "darwin":
            raise ValueError("DeviceKit PDF rendering requires macOS")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cg = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.io = C.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
        def fn(lib, name, result, *args):
            value = getattr(lib, name); value.restype = result; value.argtypes = args
            return value
        p, n, d = C.c_void_p, C.c_size_t, C.c_double
        self.url = fn(self.cf, "CFURLCreateFromFileSystemRepresentation", p, p, C.c_char_p, C.c_long, C.c_bool)
        self.string = fn(self.cf, "CFStringCreateWithCString", p, p, C.c_char_p, C.c_uint32)
        self.release = fn(self.cf, "CFRelease", None, p)
        self.document = fn(self.cg, "CGPDFDocumentCreateWithURL", p, p)
        self.release_document = fn(self.cg, "CGPDFDocumentRelease", None, p)
        self.page = fn(self.cg, "CGPDFDocumentGetPage", p, p, n)
        self.box = fn(self.cg, "CGPDFPageGetBoxRect", Rect, p, C.c_int)
        self.color = fn(self.cg, "CGColorSpaceCreateDeviceRGB", p)
        self.bitmap = fn(self.cg, "CGBitmapContextCreate", p, p, n, n, n, n, p, C.c_uint32)
        self.save = fn(self.cg, "CGContextSaveGState", None, p)
        self.restore = fn(self.cg, "CGContextRestoreGState", None, p)
        self.translate = fn(self.cg, "CGContextTranslateCTM", None, p, d, d)
        self.scale = fn(self.cg, "CGContextScaleCTM", None, p, d, d)
        self.draw = fn(self.cg, "CGContextDrawPDFPage", None, p, p)
        self.clip = fn(self.cg, "CGContextClipToRect", None, p, Rect)
        self.blend = fn(self.cg, "CGContextSetBlendMode", None, p, C.c_int)
        self.image = fn(self.cg, "CGBitmapContextCreateImage", p, p)
        self.destination = fn(self.io, "CGImageDestinationCreateWithURL", p, p, p, n, p)
        self.add = fn(self.io, "CGImageDestinationAddImage", None, p, p, p)
        self.finish = fn(self.io, "CGImageDestinationFinalize", C.c_bool, p)

    def file_url(self, path):
        raw = str(Path(path).resolve()).encode()
        value = self.url(None, raw, len(raw), False)
        if not value:
            raise ValueError("Invalid PDF path")
        return value

    def render(self, output, width, height, layers):
        """Layers are (path, x, y, width, height), measured from the top left."""
        if min(width, height) < 1 or width * height > 24_000_000:
            raise ValueError("Unsupported PDF canvas")
        owned = []
        def own(value):
            if not value:
                raise ValueError("Could not render DeviceKit PDF")
            owned.append(value); return value
        try:
            color = own(self.color())
            context = own(self.bitmap(None, width, height, 8, width * 4, color, 0x4001))
            for layer in layers:
                path, x, y, w, h = layer[:5]
                blend = layer[5] if len(layer) == 6 else 0
                url = self.file_url(path)
                try:
                    document = self.document(url)
                finally:
                    self.release(url)
                if not document:
                    raise ValueError("Unsupported DeviceKit PDF")
                try:
                    page = self.page(document, 1)
                    if not page:
                        raise ValueError("Empty DeviceKit PDF")
                    box = self.box(page, 0)
                    pw, ph = box.size.width, box.size.height
                    if not all(math.isfinite(v) and v > 0 for v in (pw, ph)):
                        raise ValueError("Invalid PDF bounds")
                    # Keep the source size for buttons; strips are the original
                    # nine-slice layout's stretchable horizontal/vertical edges.
                    w, h = w or pw, h or ph
                    self.save(context)
                    self.clip(context, Rect(Point(x, height-y-h), Size(w, h)))
                    self.blend(context, blend)
                    self.translate(context, x, height-y-h)
                    self.scale(context, w/pw, h/ph)
                    self.translate(context, -box.origin.x, -box.origin.y)
                    self.draw(context, page)
                    self.restore(context)
                finally:
                    self.release_document(document)
            image = own(self.image(context))
            url = own(self.file_url(output))
            kind = own(self.string(None, b"public.png", 0x08000100))
            destination = own(self.destination(url, kind, 1, None))
            self.add(destination, image, None)
            if not self.finish(destination):
                raise ValueError("Could not write normalized DeviceKit image")
        finally:
            for item in reversed(owned):
                self.release(item)


def render_chrome(source, body_path, mask_path):
    renderer = PDFRenderer()
    rect = source["screenRect"]
    # DeviceKit pieces include the black backing under the display. Simulator
    # draws its framebuffer above that backing. Punch the exact visible mask out
    # for our shared overlay layer instead, preserving transparent outer pixels.
    layers = [*source["pdfLayers"], (source["visibleMask"], rect["x"], rect["y"], rect["width"], rect["height"], 23)]
    renderer.render(body_path, source["bodySize"]["width"], source["bodySize"]["height"], layers)
    renderer.render(mask_path, rect["width"], rect["height"],
                    [(source["visibleMask"], 0, 0, rect["width"], rect["height"])])
