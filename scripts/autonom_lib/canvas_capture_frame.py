"""PNG/MP4 composition using the same resolved descriptor as the browser.

The source screen is never scaled. On failure the source file is left intact.
Only normalized local resources are consumed; browser supplied paths are refused.
"""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zlib
from . import errors


def options(value, repository=None):
    if value is None:
        return None
    if repository is None:
        raise errors.AutonomError(errors.INVALID_VALUE, "A target is required for framed capture")
    result = repository.snapshot(value)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise errors.AutonomError(errors.BACKEND_FAILED, "Framed captures need ffmpeg and ffprobe",
                                  "Install ffmpeg, or turn Device frame off.")
    return result


def _png(path, width, height, rows, *, gray=False):
    def chunk(kind, data):
        return struct.pack(">I", len(data))+kind+data+struct.pack(">I", zlib.crc32(kind+data)&0xffffffff)
    compressor = zlib.compressobj(6)
    with Path(path).open("wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        stream.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0 if gray else 6, 0, 0, 0)))
        for row in rows:
            data = compressor.compress(b"\0"+row)
            if data:
                stream.write(chunk(b"IDAT", data))
        stream.write(chunk(b"IDAT", compressor.flush()))
        stream.write(chunk(b"IEND", b""))


def video_duration(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "json", str(path)], capture_output=True, check=True, timeout=15)
        value = float(json.loads(result.stdout)["format"]["duration"])
        return value if 0 < value < 86400 else None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def render(path, frame, *, video=False, repository=None, duration_s=None):
    timing = video and duration_s is not None
    if timing and (not isinstance(duration_s, (int, float)) or not 0 < duration_s <= 180):
        raise errors.AutonomError(errors.INVALID_VALUE, "Invalid recording duration")
    if frame is None and not timing:
        return
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_streams", "-of", "json", str(path)],
                               capture_output=True, check=True, timeout=15)
        stream = json.loads(probe.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        rotate = next((v.get("rotation", 0) for v in stream.get("side_data_list", []) if "rotation" in v),
                      stream.get("tags", {}).get("rotate", 0))
        if abs(float(rotate)) % 180 == 90:
            width, height = height, width
        if frame is not None and repository is None:
            raise ValueError("Frame repository required")
        desc = repository.resolve(width, height, frame["rotation"], snapshot=frame) if frame else None
        framed = desc and desc["source"] != "generic" and desc["layers"]
        if not framed and not timing:
            return  # Unavailable artwork: never invent a frame.
        ow, oh = width, height
        if framed:
            rect, body = desc["screenRect"], desc["bodySize"]
            scale = width/rect["width"]
            x, y = round(rect["x"]*scale), round(rect["y"]*scale)
            ow, oh = max(width+x, round(body["width"]*scale)), max(height+y, round(body["height"]*scale))
        if video:
            ow += ow % 2
            oh += oh % 2
        with tempfile.TemporaryDirectory(prefix=".frame-", dir=path.parent) as temp:
            directory = Path(temp)
            output = directory/("capture.mp4" if video else "capture.png")
            argv = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(path)]
            # Native recorders emit frames only on changes. Preserve real timestamps
            # and hold the last image through Stop; never report wall time for a shorter MP4.
            timing_filter = (f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={duration_s},"
                             f"fps=30,trim=duration={duration_s}") if timing else "null"
            graph = f"[0:v]{timing_filter},format=rgba[raw];"
            if framed:
                argv += ["-i", desc["layers"][0]["path"], "-i", desc["screenMask"]["path"]]
                graph += (f"[1:v]scale={round(body['width']*scale)}:{round(body['height']*scale)}[body];"
                          f"[2:v]scale={width}:{height},format=rgba,alphaextract[mask];"
                          f"[raw][mask]alphamerge,pad={ow}:{oh}:{x}:{y}:color=black@0[screen];"
                          "[screen][body]overlay=0:0:format=auto[framed];")
            else:
                graph += f"[raw]pad={ow}:{oh}:0:0[framed];"
            if video:
                graph += ("[framed]split[fg][bg];[bg]lutrgb=r=255:g=255:b=255,format=rgb24[white];"
                          "[white][fg]overlay=0:0:format=auto[out]")
            else:
                graph += ("[framed]format=gbrap,geq=r='if(eq(alpha(X,Y),0),0,r(X,Y))':"
                          "g='if(eq(alpha(X,Y),0),0,g(X,Y))':b='if(eq(alpha(X,Y),0),0,b(X,Y))':"
                          "a='alpha(X,Y)',format=rgba[out]")
            argv += ["-filter_complex_threads", "1", "-filter_complex", graph, "-map", "[out]"]
            if video:
                argv += ["-map", "0:a?", "-c:a", "copy", "-c:v", "libx264", "-preset", "fast",
                         "-threads", "2", "-crf", "18", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-movflags", "+faststart"]
            else:
                argv += ["-frames:v", "1", "-pix_fmt", "rgba"]
            subprocess.run([*argv, str(output)], capture_output=True, check=True, timeout=120 if video else 30)
            if not output.is_file() or not output.stat().st_size:
                raise ValueError("Empty framed capture")
            output.replace(path)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        raise errors.AutonomError(errors.BACKEND_FAILED, "Could not render the device frame",
                                  "The original capture was preserved.") from exc
