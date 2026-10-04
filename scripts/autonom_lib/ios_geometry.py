"""Measure Simulator points without relying on an accessibility root frame."""
from __future__ import annotations

import json
import math
from pathlib import Path
import plistlib
import tempfile

from . import errors, ios_simctl, screenshot
from .platform import Target


def _main_screen(bundle: Path) -> tuple[float, tuple[int, int]] | None:
    """(scale, native pixels) of a device type's main screen.

    Xcode 27 device types keep the screen in capabilities.plist under
    ``capabilities.ScreenDimensionsCapability`` (main-screen-scale/-width/
    -height); older device types list it as the integrated entry of
    ``capabilities.displays``; older Xcodes put mainScreenScale/-Width/-Height
    in profile.plist. Read in that order, first complete record wins.
    """
    resources = bundle / "Contents/Resources"
    candidates: list[tuple[object, object, object]] = []
    try:
        capabilities = plistlib.loads((resources / "capabilities.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        capabilities = {}
    caps = capabilities.get("capabilities") if isinstance(capabilities, dict) else None
    if isinstance(caps, dict):
        dims = caps.get("ScreenDimensionsCapability")
        if isinstance(dims, dict):
            candidates.append((dims.get("main-screen-scale"), dims.get("main-screen-width"),
                               dims.get("main-screen-height")))
        displays = caps.get("displays")
        if isinstance(displays, list):
            candidates.extend((d.get("scale"), d.get("width"), d.get("height"))
                              for d in displays if isinstance(d, dict)
                              and d.get("displayType") == "integrated")
    try:
        profile = plistlib.loads((resources / "profile.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        profile = {}
    if isinstance(profile, dict):
        candidates.append((profile.get("mainScreenScale"), profile.get("mainScreenWidth"),
                           profile.get("mainScreenHeight")))
    for scale, width, height in candidates:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in (scale, width, height)):
            continue
        if not math.isfinite(float(scale)) or scale <= 0 or width <= 0 or height <= 0 \
                or not float(width).is_integer() or not float(height).is_integer():
            continue
        return float(scale), (int(width), int(height))
    return None


def measure(target: Target) -> dict | None:
    try:
        devices = json.loads(ios_simctl.run_simctl(target.tool, ["list", "devices", "--json"]).stdout)
        device = next(d for group in devices.get("devices", {}).values() for d in group
                      if d.get("udid") == target.target_id and d.get("state") == "Booted")
        types = json.loads(ios_simctl.run_simctl(target.tool, ["list", "devicetypes", "--json"]).stdout)
        kind = next(d for d in types.get("devicetypes", [])
                    if d.get("identifier") == device.get("deviceTypeIdentifier"))
        screen = _main_screen(Path(kind["bundlePath"]))
        if screen is None:
            return None
        scale, native = screen
        # The PNG supplies current orientation, not the default profile layout.
        with tempfile.TemporaryDirectory(prefix="autonom-geometry-") as directory:
            path = Path(directory) / "screen.png"
            ios_simctl.screenshot(target.tool, target.target_id, path)
            pixels = screenshot.png_size(path)
        if pixels not in {native, native[::-1]}:
            return None
        points = [value / scale for value in pixels]
        if not all(value.is_integer() and value > 0 for value in points):
            return None
        return {"width": int(points[0]), "height": int(points[1]), "scale": scale,
                "pixels": list(pixels), "units": "points", "source": "simctl.profile+png",
                "orientation": "landscape" if pixels[0] > pixels[1] else "portrait"}
    except (OSError, ValueError, KeyError, StopIteration, errors.AutonomError):
        return None
