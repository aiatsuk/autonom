#!/usr/bin/env python3
"""Install the GitHub release matching this plugin; stdlib only, Python 3.11+."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024
MARKER = "# Autonom release launcher"


class InstallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InstallError("invalid_argument", message)


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parents[3] / ".codex-plugin/plugin.json"
    version = json.loads(manifest.read_text())["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise InstallError("invalid_version", "Plugin needs a stable semantic version.")
    return version


def download(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "autonom-setup"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if not response.url.startswith("https://"):
                raise InstallError("download_failed", "Release redirected outside HTTPS.")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise InstallError(
            "release_unavailable",
            f"GitHub returned HTTP {exc.code}. The matching release must be published; "
            "retry later or install the plugin from a published release tag.",
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise InstallError("download_failed", f"Cannot download release: {exc}") from exc
    if len(data) > limit:
        raise InstallError("download_too_large", "Release download exceeds size limit.")
    return data


def expected_checksum(text: str, name: str) -> str:
    matches = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == name:
            matches.append(fields[0])
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        raise InstallError("invalid_checksum", f"Expected one SHA-256 entry for {name}.")
    return matches[0].lower()


def extract(archive: Path, destination: Path, name: str) -> Path:
    """Validate every member before writing; never extract links or special files."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = []
        seen: set[str] = set()
        total = 0
        for member in bundle:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != name or "\\" in member.name
                    or not (member.isdir() or member.isfile())
                    or str(path) in seen or member.size < 0):
                raise InstallError("unsafe_archive", f"Unsafe archive entry: {member.name}")
            seen.add(str(path))
            total += member.size
            members.append(member)
            if total > MAX_UNPACKED or len(members) > 20000:
                raise InstallError("unsafe_archive", "Release exceeds extraction limits.")
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise InstallError("unsafe_archive", "Unreadable archive member.")
                with source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    runtime = destination / name
    if not (runtime / "scripts/autonom.py").is_file():
        raise InstallError("invalid_bundle", "Release is missing scripts/autonom.py.")
    return runtime


def run_cli(runtime: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-B", str(runtime / "scripts/autonom.py"), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise InstallError("runtime_check_failed", "Runtime did not return valid JSON.") from exc
    if result.returncode or not isinstance(payload, dict):
        raise InstallError("runtime_check_failed", "Runtime verification failed.")
    return payload


def check_launcher(launcher: Path) -> None:
    if not launcher.exists() and not launcher.is_symlink():
        return
    # Recognize the maintained-checkout link and the two launchers we ship.
    if launcher.is_symlink():
        target = launcher.resolve()
        if (target.name == "autonom.py" and target.parent.name == "scripts"
                and (target.parent / "autonom_lib/__init__.py").is_file()):
            return
    elif launcher.is_file():
        with launcher.open() as source:
            header = source.read(4096).splitlines()
        if len(header) > 1 and (header[1] == MARKER
                               or header[1].startswith("# autonom launcher (copy mode) -> ")):
            return
    raise InstallError("destination_exists", f"Refusing to replace unrelated file: {launcher}")


def tree_digest(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise InstallError("destination_exists", f"Unexpected runtime entry: {path}")
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def check_session_store() -> None:
    """The normal session listing tolerates damaged pointers; setup must not."""
    root = Path(os.environ.get("AUTONOM_HOME") or Path.home() / ".autonom") / "sessions"
    if not root.exists():
        return

    def read_record(path: Path) -> dict:
        try:
            with path.open("rb") as source:
                data = source.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("record too large")
            record = json.loads(data)
            if (not isinstance(record, dict)
                    or not isinstance(record.get("session_id"), str)
                    or not re.fullmatch(r"s_[A-Za-z0-9]+", record["session_id"])
                    or record.get("platform", "android") not in ("android", "ios")
                    or not isinstance(record.get("target_id") or record.get("serial")
                                      or record.get("udid"), str)):
                raise ValueError("invalid session identity")
            return record
        except (OSError, ValueError, RecursionError) as exc:
            raise InstallError("session_check_failed", f"Cannot verify session file: {path}") from exc

    for path in root.iterdir():
        if path.name != "current.json" and not (
                path.name.startswith("target-") and path.name.endswith(".json")):
            continue
        pointer = read_record(path)
        record = read_record(root / pointer["session_id"] / "session.json")
        def identity(value: dict) -> tuple:
            return (value["session_id"], value.get("platform", "android"),
                    value.get("target_id") or value.get("serial") or value.get("udid"))
        if identity(record) != identity(pointer):
            raise InstallError("session_check_failed", f"Session identity mismatch: {path}")
        if not record.get("stopped_at"):
            raise InstallError("active_session", "Finish active device sessions before installing.")


def install(version: str, runtime_dir: Path, bin_dir: Path) -> dict:
    name = f"autonom-{version}"
    asset = f"{name}.tgz"
    base = f"https://github.com/aiatsuk/autonom/releases/download/v{version}"
    launcher = bin_dir / "autonom"
    check_launcher(launcher)
    check_session_store()
    checksum = expected_checksum(download(f"{base}/SHA256SUMS", 64 * 1024).decode(), asset)
    data = download(f"{base}/{asset}", MAX_DOWNLOAD)
    if hashlib.sha256(data).hexdigest() != checksum:
        raise InstallError("checksum_mismatch", "Release SHA-256 does not match SHA256SUMS.")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Retain previous versions: a failed install never removes a working runtime.
    final = runtime_dir / f"{version}-{checksum[:16]}"
    with tempfile.TemporaryDirectory(prefix=".autonom-stage-", dir=runtime_dir) as stage:
        staging = Path(stage)
        archive = staging / asset
        archive.write_bytes(data)
        runtime = extract(archive, staging, name)
        if run_cli(runtime, "version").get("version") != version:
            raise InstallError("version_mismatch", "Release runtime and plugin versions differ.")
        sessions = run_cli(runtime, "session", "list")
        if (sessions.get("ok") is not True or not isinstance(sessions.get("sessions"), list)
                or sessions.get("warnings")):
            raise InstallError("session_check_failed", "Cannot establish device session state.")
        if sessions["sessions"]:
            raise InstallError("active_session", "Finish active device sessions before installing.")
        if final.is_symlink():
            raise InstallError("destination_exists", f"Runtime destination is a symlink: {final}")
        if final.exists():
            if not final.is_dir() or tree_digest(final) != tree_digest(runtime):
                raise InstallError("destination_exists", f"Existing runtime differs: {final}")
        else:
            runtime.rename(final)
        check_session_store()
        check_launcher(launcher)
        # Absolute interpreter and runtime paths also work in GUI apps with a short PATH.
        content = (f"#!/bin/sh\n{MARKER}\nexec {shlex.quote(sys.executable)} -B "
                   f"{shlex.quote(str(final / 'scripts/autonom.py'))} \"$@\"\n")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", prefix=".autonom-", dir=bin_dir,
                                             delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
            temporary.chmod(0o755)
            os.replace(temporary, launcher)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"ok": True, "version": version, "runtime": str(final),
            "executable": str(launcher), "sha256": checksum,
            "on_path": str(bin_dir) in os.environ.get("PATH", "").split(os.pathsep),
            "next": f"{shlex.quote(str(launcher))} doctor"}


def main() -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path,
                        default=Path.home() / ".local/share/autonom-runtimes")
    parser.add_argument("--bin-dir", type=Path,
                        default=Path(os.environ.get("AUTONOM_BIN_DIR", Path.home() / ".local/bin")))
    try:
        args = parser.parse_args()
        if sys.version_info < (3, 11):
            raise InstallError("python_too_old", "Use Python 3.11 or newer.")
        result = install(plugin_version(), args.runtime_dir.expanduser().absolute(),
                         args.bin_dir.expanduser().absolute())
    except (InstallError, OSError, ValueError, KeyError, EOFError, tarfile.TarError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error_code": getattr(exc, "code", "install_failed"),
                          "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
