"""Local device-frame adapters and the geometry contract used by UI and export.

SDK/Xcode artwork never ships with Autonom. Immutable normalized artifacts live
under the user's cache, keyed by source content + adapter version.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile

from . import emulator, errors

VERSION = 5
MAX_PIXELS = 24_000_000


def command(argv, timeout=20):
    return subprocess.check_output(argv, stderr=subprocess.PIPE, timeout=timeout)


def dimensions(path):
    stream = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))["streams"][0]
    w, h = int(stream["width"]), int(stream["height"])
    if min(w, h) < 1 or w * h > MAX_PIXELS:
        raise ValueError("unsupported image dimensions")
    return w, h


def rgba(path):
    w, h = dimensions(path)
    pixels = command(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
                      "-f", "rawvideo", "-pix_fmt", "rgba", "-"])
    if len(pixels) != w * h * 4:
        raise ValueError("incomplete image")
    return w, h, pixels


def parse_layout(text):
    """Strict subset of Emulator skin grammar; unknown layouts fall back."""
    tokens = re.findall(r'"[^"]*"|[{}]|[^\s{}#]+', re.sub(r"#[^\n]*", "", text))
    index = 0

    def block(nested=False):
        nonlocal index
        result = {}
        while index < len(tokens):
            key = tokens[index]
            index += 1
            if key == "}":
                if not nested:
                    raise ValueError("unexpected closing brace")
                return result
            if index >= len(tokens) or key in result:
                raise ValueError("incomplete or duplicate layout key")
            value = tokens[index]
            index += 1
            result[key] = block(True) if value == "{" else value.strip('"')
        if nested:
            raise ValueError("unclosed layout block")
        return result
    return block()


def asset(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() not in (".png", ".webp"):
        raise ValueError("unsupported skin asset")
    if path.stat().st_size > 16_000_000:
        raise ValueError("skin asset too large")
    return path


def android_source(target, model_override=None, variant=None):
    # Explicit models use local SDK artwork and must not depend on a responsive AVD.
    name, config = None, {}
    if not model_override:
        name = emulator.running_avd_name(target.tool, target.target_id)
        if not name:
            raise ValueError("No AVD skin for this device")
        info = emulator.describe_avd(name)
        config = emulator._read_ini(Path(info["path"]) / "config.ini") if info.get("path") else {}
    model = model_override or config.get("hw.device.name", "")
    roots = [Path(target.tool).resolve().parent.parent]
    roots += [Path(os.environ[key]).expanduser() for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME") if os.environ.get(key)]
    roots += [Path.home() / "Library/Android/sdk", Path.home() / "Android/Sdk"]
    # Respect the selected skin; do not silently replace an unavailable explicit skin.
    selected = None if model_override else config.get("skin.path")
    if selected and selected != "_no_skin":
        choices = [Path(selected).expanduser()]
        choices += [root / selected for root in roots]
    elif selected == "_no_skin":
        raise ValueError("AVD has no skin")
    else:
        skin = model if model_override else config.get("skin.name") or model
        if not skin or Path(skin).name != skin:
            raise ValueError("Unknown AVD model")
        choices = [root / "skins" / skin for root in roots]
    if variant:
        if variant not in ("default", "closed"):
            raise ValueError("Unsupported skin variant")
        choices = [p / variant for p in choices]
    root = next((p for p in choices if (p / "layout").is_file()), None)
    if root is None:
        raise ValueError("AVD skin is not installed")
    layout = parse_layout((root / "layout").read_text())
    # Each Fold state has a separate single-display skin. Reject multi-display layouts.
    parts, layouts = layout["parts"], layout["layouts"]
    portrait = layouts.get("portrait") or layouts.get("landscape")
    if not portrait:
        raise ValueError("Unsupported skin orientation")
    slots = [v for k, v in portrait.items() if k.startswith("part") and isinstance(v, dict)]
    displays = [(slot, parts[slot["name"]]["display"]) for slot in slots if "display" in parts.get(slot.get("name"), {})]
    backgrounds = [(slot, parts[slot["name"]]) for slot in slots if "background" in parts.get(slot.get("name"), {})]
    if len(displays) != 1 or len(backgrounds) != 1 or len(slots) != 2:
        raise ValueError("Unsupported multi-part skin")
    slot, display = displays[0]
    bg_slot, bg = backgrounds[0]
    if int(bg_slot.get("x", 0)) or int(bg_slot.get("y", 0)):
        raise ValueError("Unsupported skin background offset")
    rect = dict(x=int(slot.get("x", 0)) + int(display.get("x", 0)),
                y=int(slot.get("y", 0)) + int(display.get("y", 0)),
                width=int(display["width"]), height=int(display["height"]))
    body = dict(width=int(portrait["width"]), height=int(portrait["height"]))
    if min(rect.values()) < 0 or min(rect["width"], rect["height"]) < 1 or body["width"] * body["height"] > MAX_PIXELS:
        raise ValueError("Invalid skin geometry")
    if rect["x"] + rect["width"] > body["width"] or rect["y"] + rect["height"] > body["height"]:
        raise ValueError("Screen outside skin")
    back = asset(root, bg["background"]["image"])
    mask_name = bg.get("foreground", {}).get("mask")
    mask = asset(root, mask_name) if mask_name else None
    # Older official skins (e.g. Nexus 5) have a rectangular display and an
    # optional decorative onion overlay, not a display occlusion mask.
    back_width, back_height = dimensions(back)
    if (back_width < body["width"] or back_height < body["height"]
            or back_width * back_height > MAX_PIXELS
            or (mask is not None and dimensions(mask) != (rect["width"], rect["height"]))):
        raise ValueError("Skin image dimensions differ from layout")
    # Official artwork can extend past the layout window (Pixel 10 includes a few
    # extra border pixels). Retain the whole image without moving/resizing the display.
    body = dict(width=back_width, height=back_height)
    return dict(model=model.replace("_", " ").title() or name, source="android-sdk-skin",
                bodySize=body, screenRect=rect, back=back, mask=mask,
                files=[root / "layout", back] + ([mask] if mask else []))


def android_rotation(target):
    """Read the primary display only. Android rotation is counter-clockwise."""
    text = command([target.tool, "-s", target.target_id, "shell", "dumpsys", "window", "displays"], timeout=5).decode()
    block = re.search(r"(?:^|\n)\s*Display: mDisplayId=0\b([\s\S]*?)(?=\n\s*Display: mDisplayId=|$)", text)
    found = re.search(r"^\s*mRotation=(?:ROTATION_)?(270|180|90|[0-3])\b", block[1], re.M) if block else None
    if not found:
        raise ValueError("Primary display orientation unavailable")
    value = int(found[1])
    return -(value if value > 3 else value * 90) % 360


def ios_source(target):
    """Discover the selected model through simctl, never through stream aspect."""
    devices = json.loads(command([target.tool, "simctl", "list", "devices", "-j"]))
    device = next((d for group in devices["devices"].values() for d in group if d["udid"] == target.target_id), None)
    if not device:
        raise ValueError("Simulator profile unavailable")
    types = json.loads(command([target.tool, "simctl", "list", "devicetypes", "-j"]))["devicetypes"]
    kind = next((t for t in types if t["identifier"] == device.get("deviceTypeIdentifier")), None)
    if not kind:
        raise ValueError("Simulator device type unavailable")
    profile_path = Path(kind["bundlePath"]) / "Contents/Resources/profile.plist"
    with profile_path.open("rb") as stream:
        profile = plistlib.load(stream)
    source = dict(model=kind["name"], source="generic", profileSource="simulator-profile",
                  profileId=kind["identifier"], chromeIdentifier=profile.get("chromeIdentifier"),
                  fallbackReason="Original full body is unavailable for this Simulator model")
    try:
        source.update(ios_chrome(profile_path, profile))
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        pass  # Unsupported/missing local artwork never prevents a Simulator stream.
    return source


def pdf_asset(root, name):
    path = (root / (name + ".pdf")).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size > 16_000_000:
        raise ValueError("Unsupported DeviceKit asset")
    return path


def ios_chrome(profile_path, profile, roots=None):
    """Match the profile's exact bundle identifier in the installed DeviceKit.

    These are Simulator's nine-slice resources, not Apple marketing bezels.
    Screen dimensions and layout metrics come exclusively from the two profiles.
    """
    if roots is None:
        roots = [parent/"DeviceKit/Chrome" for parent in profile_path.parents if parent.name == "Developer"]
        roots += [Path("/Library/Developer/DeviceKit/Chrome"), Path.home()/"Library/Developer/DeviceKit/Chrome"]
        if os.environ.get("DEVELOPER_DIR"):
            roots.insert(0, Path(os.environ["DEVELOPER_DIR"])/"Library/DeviceKit/Chrome")
    identifier = profile["chromeIdentifier"]
    bundle = None
    for root in dict.fromkeys(roots):
        for candidate in root.glob("*.devicechrome"):
            with (candidate/"Contents/Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
            if info.get("CFBundleIdentifier") == identifier:
                bundle = candidate
                break
        if bundle:
            break
    if bundle is None:
        raise ValueError("DeviceKit body unavailable")
    root = bundle/"Contents/Resources"
    chrome_path = root/"chrome.json"
    chrome = json.loads(chrome_path.read_text())
    if chrome["identifier"] != identifier:
        raise ValueError("DeviceKit identifier mismatch")
    images = chrome["images"]
    sizing, padding = images["sizing"], images["devicePadding"]
    scale = float(profile["mainScreenScale"])
    width, height = int(profile["mainScreenWidth"]), int(profile["mainScreenHeight"])
    if not 1 <= scale <= 4:
        raise ValueError("Unsupported Simulator scale")
    valid_size(width, height)
    left, right, top, bottom = [float(sizing[k]) for k in ("leftWidth","rightWidth","topHeight","bottomHeight")]
    pl, pr, pt, pb = [float(padding[k]) for k in ("left","right","top","bottom")]
    if not all(math.isfinite(v) and 0 <= v <= 200 for v in (left,right,top,bottom,pl,pr,pt,pb)):
        raise ValueError("Unsupported DeviceKit margins")
    bw, bh = width/scale+left+right, height/scale+top+bottom
    # Piece extents are stored in the source PDFs; they are not the border width.
    # The supported profile format uses ordinary MediaBox rectangles.
    def bounds(path):
        found = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", path.read_bytes())
        if not found:
            raise ValueError("Unsupported DeviceKit PDF bounds")
        return tuple(float(v) for v in found.groups())
    pieces = {key:pdf_asset(root, images[key]) for key in
              ("topLeft","top","topRight","right","bottomRight","bottom","bottomLeft","left")}
    cw, ch = bounds(pieces["topLeft"])
    if min(cw,ch) <= 0 or bw < cw*2 or bh < ch*2:
        raise ValueError("Unsupported DeviceKit nine-slice geometry")
    for key in ("topRight","bottomRight","bottomLeft"):
        if bounds(pieces[key]) != (cw,ch):
            raise ValueError("Asymmetric DeviceKit corners unsupported")
    positions = {"topLeft":(0,0,cw,ch),"top":(cw,0,bw-cw*2,ch),"topRight":(bw-cw,0,cw,ch),
                 "right":(bw-cw,ch,cw,bh-ch*2),"bottomRight":(bw-cw,bh-ch,cw,ch),
                 "bottom":(cw,bh-ch,bw-cw*2,ch),"bottomLeft":(0,bh-ch,cw,ch),"left":(0,ch,cw,bh-ch*2)}
    layers, files = [], [profile_path, chrome_path, bundle/"Contents/Info.plist", *pieces.values()]
    body = dict(width=round((bw+pl+pr)*scale),height=round((bh+pt+pb)*scale))
    valid_size(*body.values())
    for control in chrome.get("inputs", []):
        if control.get("type") != "button" or control.get("onTop") or control.get("align") != "leading" or control.get("anchor") not in ("left","right"):
            raise ValueError("Unsupported DeviceKit button layout")
        path = pdf_asset(root,control["image"]); files.append(path)
        w,h = bounds(path); offset = control["offsets"]["normal"]
        x = float(offset["x"])
        if control["anchor"] == "right":
            x += bw+pl+pr-w
        layers.append((path,x*scale,float(offset["y"])*scale,w*scale,h*scale))
    layers += [(pieces[key],(x+pl)*scale,(y+pt)*scale,w*scale,h*scale)
               for key,(x,y,w,h) in positions.items()]
    mask = pdf_asset(profile_path.parent,profile["framebufferMask"]); files.append(mask)
    if bounds(mask) != (width,height):
        raise ValueError("Simulator mask dimensions differ from the profile")
    return dict(source="apple-devicekit",bodySize=body,
                screenRect=dict(x=round((pl+left)*scale),y=round((pt+top)*scale),width=width,height=height),
                pdfLayers=layers,visibleMask=mask,files=files,fallbackReason=None)


def oriented_rect(body, rect, angle):
    w, h = body["width"], body["height"]
    x, y, sw, sh = (rect[k] for k in ("x", "y", "width", "height"))
    if angle == 90:
        return dict(width=h, height=w), dict(x=h-y-sh, y=x, width=sh, height=sw)
    if angle == 180:
        return body.copy(), dict(x=w-x-sw, y=h-y-sh, width=sw, height=sh)
    if angle == 270:
        return dict(width=h, height=w), dict(x=y, y=w-x-sw, width=sh, height=sw)
    return body.copy(), rect.copy()


def rotation_filter(angle):
    return {0: "null", 90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}[angle]


def valid_size(w, h):
    if type(w) not in (int, float) or type(h) not in (int, float) or not all(math.isfinite(v) and 1 <= v <= 8192 for v in (w, h)) or w*h > MAX_PIXELS:
        raise ValueError("Invalid screen dimensions")
    return round(w), round(h)


def generic_geometry(w, h, reason, model="Unknown"):
    # Neutral, not an imitation of any phone: no invented buttons or camera.
    scale = 1000 / min(w, h)
    sw, sh = round(w*scale), round(h*scale)
    inset = 18
    return dict(id="generic", model=model, source="generic", bodySize=dict(width=sw+inset*2, height=sh+inset*2),
                screenRect=dict(x=inset, y=inset, width=sw, height=sh), layers=[],
                screenMask=None, borderRadius=inset*2, orientations=[0, 90, 180, 270], rotation=0,
                fallbackReason=reason)


# Explicit UI model selections must never fall back to the running AVD model.
PRESET_MODELS = {
    "small": ("nexus_5", "Small phone (Nexus 5)"),
    "pixel-10": ("pixel_10", "Pixel 10"),
    "pixel-fold": ("pixel_fold", "Pixel Fold (open)"),
    "pixel-fold-closed": ("pixel_fold", "Pixel Fold (folded)"),
    "tablet": ("pixel_tablet", "Pixel Tablet"),
}

def frame_preset(value):
    if value is None or value in ("default", "other", "restore", ""):
        return None
    if not isinstance(value, str) or value not in PRESET_MODELS:
        raise errors.AutonomError(errors.INVALID_VALUE, "Invalid frame model")
    return value


class FrameRepository:
    def __init__(self, target, cache=None):
        self.target = target
        self.cache = Path(cache) if cache else Path(os.environ.get("XDG_CACHE_HOME", Path.home()/".cache")) / "autonom/device-frames"
        self.source = None
        self.variants = {}
        self.model_sources = {}

    def discover(self, preset=None):
        preset = frame_preset(preset)
        if preset and self.target and self.target.platform == "android":
            if preset not in self.model_sources:
                model, label = PRESET_MODELS[preset]
                try:
                    variant = {"pixel-fold": "default", "pixel-fold-closed": "closed"}.get(preset)
                    source = (android_source(self.target, model_override=model, variant=variant) if variant
                              else android_source(self.target, model_override=model))
                    source["model"] = label
                    digest = hashlib.sha256(str(VERSION).encode())
                    for path in source["files"]:
                        digest.update(str(path.resolve()).encode())
                        digest.update(path.read_bytes())
                    source["id"] = digest.hexdigest()
                    self.model_sources[preset] = source
                except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError):
                    self.model_sources[preset] = dict(model=label, source="generic",
                        fallbackReason="Original frame is not installed for this model")
            return self.model_sources[preset]
        if self.source is not None:
            return self.source
        try:
            source = android_source(self.target) if self.target.platform == "android" else ios_source(self.target)
            if source["source"] != "generic":
                digest = hashlib.sha256(str(VERSION).encode())
                for path in source["files"]:
                    digest.update(str(path.resolve()).encode())
                    digest.update(path.read_bytes())
                source["id"] = digest.hexdigest()
            self.source = source
        except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as exc:
            self.source = dict(model="Unknown", source="generic", fallbackReason=str(exc)[:160] or "Unsupported resources")
        return self.source

    def snapshot(self, value):
        if value is None:
            return None
        if not isinstance(value, dict) or (not {"mode", "rotation"} <= set(value) or set(value) - {"mode", "rotation", "preset"}) or value.get("mode") != "auto" or type(value.get("rotation")) is not int or value["rotation"] not in (0, 90, 180, 270):
            raise errors.AutonomError(errors.INVALID_VALUE, "Invalid device frame selection")
        # Resolve now; the recording retains this model even if discovery changes.
        return dict(source=deepcopy(self.discover(value.get("preset"))), rotation=value["rotation"])

    def resolve(self, width, height, angle=0, snapshot=None):
        w, h = valid_size(width, height)
        if type(angle) is not int or angle not in (0, 90, 180, 270):
            raise ValueError("Invalid frame orientation")
        source = (snapshot or {}).get("source") or self.discover()
        if source["source"] == "generic":
            return generic_geometry(w, h, source["fallbackReason"], source["model"])
        rect = source["screenRect"]
        body, screen = oriented_rect(source["bodySize"], rect, angle)
        if abs(w/h - screen["width"]/screen["height"]) > .003 * screen["width"]/screen["height"]:
            return generic_geometry(w, h, "Screen proportions differ from the selected model", source["model"])
        key = (source["id"], angle)
        if key not in self.variants:
            self.variants[key] = self.normalize(source, angle, body, screen)
        return deepcopy(self.variants[key])

    def normalize(self, source, angle, body, screen):
        directory = self.cache / source["id"]
        directory.mkdir(parents=True, exist_ok=True)
        overlay, mask = directory / f"body-{angle}.png", directory / f"mask-{angle}.png"
        if not overlay.is_file() or not mask.is_file():
            with tempfile.TemporaryDirectory(dir=directory) as work:
                work = Path(work)
                rect = source["screenRect"]
                if source["source"] == "apple-devicekit":
                    from .device_frame_pdf import render_chrome
                    render_chrome(source, work/"original-body.png", work/"visible-mask.png")
                    command(["ffmpeg", "-v", "error", "-i", str(work/"original-body.png"),
                             "-vf", rotation_filter(angle), "-frames:v", "1", str(work/"body.png")])
                    command(["ffmpeg", "-v", "error", "-i", str(work/"visible-mask.png"),
                             "-vf", f"format=rgba,alphaextract,{rotation_filter(angle)},format=rgba,geq=r=255:g=255:b=255:a='r(X,Y)'",
                             "-frames:v", "1", str(work/"mask.png")])
                else:
                    self.normalize_android(source, angle, work, rect)
                (work/"body.png").replace(overlay)
                (work/"mask.png").replace(mask)
        return dict(id=source["id"], model=source["model"], source=source["source"],
                    bodySize=body, screenRect=screen, layers=[dict(path=str(overlay), order=1)],
                    screenMask=dict(path=str(mask)), orientations=[0, 90, 180, 270],
                    rotation=angle, fallbackReason=None)

    @staticmethod
    def normalize_android(source, angle, work, rect):
        if source.get("mask") is None:
            # The declared rectangular display is fully visible; do not apply
            # old skins' optional glare/onion artwork over application pixels.
            command(["ffmpeg", "-v", "error", "-i", str(source["back"]),
                     "-vf", (f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                             f"a='if(between(X,{rect['x']},{rect['x']+rect['width']-1})*"
                             f"between(Y,{rect['y']},{rect['y']+rect['height']-1}),0,alpha(X,Y))',"
                             f"{rotation_filter(angle)}"),
                     "-frames:v", "1", str(work/"body.png")])
            command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                     f"color=white:s={rect['width']}x{rect['height']}",
                     "-vf", f"{rotation_filter(angle)},format=rgba",
                     "-frames:v", "1", str(work/"mask.png")])
            return
        # SDK mask is an occlusion overlay, with transparent visible screen.
        command(["ffmpeg", "-v", "error", "-i", str(source["back"]), "-i", str(source["mask"]),
                 "-filter_complex", f'[0:v][1:v]overlay={rect["x"]}:{rect["y"]}:format=auto,{rotation_filter(angle)}',
                 "-frames:v", "1", str(work/"body.png")])
        command(["ffmpeg", "-v", "error", "-i", str(source["mask"]), "-vf",
                 f"format=rgba,alphaextract,negate,{rotation_filter(angle)},format=rgba,geq=r=255:g=255:b=255:a='r(X,Y)'",
                 "-frames:v", "1", str(work/"mask.png")])

    def describe(self, payload):
        try:
            w, h = valid_size(payload.get("width"), payload.get("height"))
            preset = frame_preset(payload.get("preset"))
            angle = payload.get("rotation", 0)
            if type(angle) is not int or angle not in (0, 90, 180, 270):
                raise ValueError("Invalid frame orientation")
        except (ValueError, AttributeError):
            raise errors.AutonomError(errors.INVALID_VALUE, "Invalid frame geometry") from None
        if self.target and self.target.platform == "android":
            try:
                angle = android_rotation(self.target)
            except (OSError, ValueError, subprocess.SubprocessError):
                # Never choose the camera's side from the screen aspect ratio.
                return generic_geometry(w, h, "Device orientation unavailable", self.discover(preset)["model"])
        try:
            desc = self.resolve(w, h, angle, snapshot={"source": self.discover(preset)})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            desc = generic_geometry(w, h, "Frame resources could not be decoded", self.discover(preset)["model"])
        # Paths never cross the browser boundary; arbitrary files cannot be requested.
        for resource in [*desc["layers"], *([desc["screenMask"]] if desc["screenMask"] else [])]:
            path = Path(resource.pop("path"))
            resource["image"] = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        return desc
