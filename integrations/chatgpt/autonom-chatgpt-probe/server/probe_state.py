"""State files of the Autonom probe (contract 2.7).

``<state>/chatgpt-probe`` (0700) holds ``note.json``, ``selection.json``,
``media.json`` and ``results/<run_id>.json``, every file 0600 and written
through a temp file plus ``os.replace``. Read-modify-write runs under an
exclusive ``flock`` on ``state.lock``. Missing or corrupt files read as
revision 0 / empty and are replaced on the next write.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from probe_env import ProbeError, is_int

NOTE = "note.json"
SELECTION = "selection.json"
MEDIA = "media.json"
LOCK = "state.lock"
RESULTS = "results"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unwritable(detail: str) -> ProbeError:
    return ProbeError("state_unwritable", "The probe state directory cannot be written (%s)" % detail)


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    # -- plumbing

    def ensure(self, sub: Optional[str] = None) -> Path:
        path = self.directory / sub if sub else self.directory
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            if sub:
                os.chmod(path, 0o700)
        except OSError as exc:
            raise _unwritable(type(exc).__name__)
        return path

    @contextlib.contextmanager
    def lock(self, timeout: Optional[float] = None) -> Iterator[None]:
        """Exclusive state lock. With ``timeout``, raise TimeoutError when busy."""
        self.ensure()
        try:
            fd = os.open(str(self.directory / LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise _unwritable(type(exc).__name__)
        try:
            if timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("state lock busy")
                        time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read_json(self, name: str) -> Optional[Any]:
        try:
            text = (self.directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            return None

    def write_text(self, name: str, text: str, sub: Optional[str] = None) -> int:
        directory = self.ensure(sub)
        data = text.encode("utf-8")
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=str(directory))
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, str(directory / name))
            tmp_path = None
        except OSError as exc:
            raise _unwritable(type(exc).__name__)
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
        return len(data)

    def write_json(self, name: str, value: Any) -> int:
        return self.write_text(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def remove(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(self.directory / name))

    # -- note and selection

    def read_note(self) -> Dict[str, Any]:
        raw = self.read_json(NOTE)
        if (isinstance(raw, dict) and is_int(raw.get("revision")) and raw["revision"] >= 0
                and isinstance(raw.get("text"), str)):
            updated = raw.get("updated_at")
            return {"revision": raw["revision"], "text": raw["text"],
                    "updated_at": updated if isinstance(updated, str) else None}
        return {"revision": 0, "text": "", "updated_at": None}

    def read_selection(self) -> Dict[str, Any]:
        raw = self.read_json(SELECTION)
        if isinstance(raw, dict) and is_int(raw.get("revision")) and raw["revision"] >= 0:
            ref = raw.get("target_ref")
            updated = raw.get("updated_at")
            return {"target_ref": ref if isinstance(ref, str) else None, "revision": raw["revision"],
                    "updated_at": updated if isinstance(updated, str) else None}
        return {"target_ref": None, "revision": 0, "updated_at": None}

    def write_note(self, text: str, expected_revision: int, selection_revision: Optional[int],
                   operation_id: str) -> Dict[str, Any]:
        with self.lock():
            selection = self.read_selection()
            if selection_revision is not None and selection_revision != selection["revision"]:
                raise ProbeError(
                    "stale_selection",
                    "selection_revision %d is not the current selection revision %d"
                    % (selection_revision, selection["revision"]),
                    current_selection_revision=selection["revision"])
            note = self.read_note()
            if expected_revision != note["revision"]:
                raise ProbeError(
                    "stale_revision",
                    "expected_revision %d is not the current note revision %d"
                    % (expected_revision, note["revision"]),
                    current_revision=note["revision"])
            new = {"schema": 1, "revision": note["revision"] + 1, "text": text,
                   "updated_at": utc_now(), "operation_id": operation_id}
            self.write_json(NOTE, new)
        return {"note": {"revision": new["revision"], "text": text, "updated_at": new["updated_at"]},
                "selection": {"target_ref": selection["target_ref"], "revision": selection["revision"]}}

    def select(self, target_ref: str, expected_revision: int) -> Dict[str, Any]:
        with self.lock():
            selection = self.read_selection()
            if expected_revision != selection["revision"]:
                raise ProbeError(
                    "stale_selection",
                    "expected_revision %d is not the current selection revision %d"
                    % (expected_revision, selection["revision"]),
                    current_selection_revision=selection["revision"])
            new = {"schema": 1, "target_ref": target_ref, "revision": selection["revision"] + 1,
                   "updated_at": utc_now()}
            self.write_json(SELECTION, new)
        return {"target_ref": target_ref, "revision": new["revision"]}

    # -- results

    def save_results(self, run_id: str, text: str) -> int:
        return self.write_text(run_id + ".json", text, sub=RESULTS)

    # -- shared media child record (contract 2.6)

    def read_media(self) -> Optional[Dict[str, Any]]:
        raw = self.read_json(MEDIA)
        if (isinstance(raw, dict) and raw.get("schema") == 1 and is_int(raw.get("pid"))
                and raw["pid"] > 0 and is_int(raw.get("port")) and 0 < raw["port"] <= 65535
                and isinstance(raw.get("token"), str) and raw["token"]):
            return raw
        return None

    def media_file_exists(self) -> bool:
        return (self.directory / MEDIA).exists()

    def write_media(self, pid: int, port: int, token: str, owner_pid: int) -> None:
        self.write_json(MEDIA, {"schema": 1, "pid": pid, "port": port, "token": token,
                                "owner_pid": owner_pid, "started_at": utc_now()})

    def remove_media_if(self, pid: int) -> None:
        """Remove ``media.json`` only when it still records ``pid``."""
        raw = self.read_json(MEDIA)
        if isinstance(raw, dict) and raw.get("pid") == pid:
            self.remove(MEDIA)
