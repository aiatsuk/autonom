"""The seven probe tools and their result envelope (contract 2.4 and 2.5).

Every call that reaches a known tool returns a ``CallToolResult`` whose
``structuredContent`` carries ``schema_version``, ``operation_id`` and ``ok``,
whose ``content[0].text`` is the sorted JSON dump of ``structuredContent``,
and whose ``isError`` is true exactly when ``ok`` is false. The media token
and the probe nonce only ever travel in ``_meta["autonom/probe"]``.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import http.client
import json
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from probe_env import (PROBE_VERSION, SCHEMA_VERSION, TARGET_REF, ProbeError, Resolver,
                       child_env, is_int, is_number)
from probe_media_child import MediaChild
from probe_state import StateStore

UI_URI = "ui://autonom-probe/probe.html"
NONCE_WORDS = ("lime", "plum", "pear", "kiwi", "fig", "sage", "mint", "teal",
               "rust", "jade", "onyx", "ruby", "iris", "fern", "moss", "opal")
RUN_ID = re.compile(r"^probe-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
CONTROL = re.compile("[\u0000-\u001f\u007f]")
MAX_RESULTS_BYTES = 512 * 1024
MAX_DEVICES = 64
DEVICE_KEYS = ("platform", "target_id", "state", "name", "avd")

OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "operation_id", "ok"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "operation_id": {"type": "string", "pattern": "^op_[0-9a-f]{16}$"},
        "ok": {"type": "boolean"},
        "error_code": {"type": "string"},
        "error": {"type": "string"},
        "hint": {"type": "string"},
    },
    "additionalProperties": True,
}

APP_ONLY_META: Dict[str, Any] = {
    "ui": {"visibility": ["app"]},
    "openai/visibility": "private",
    "openai/widgetAccessible": True,
}

EMPTY_INPUT: Dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
REVISION = {"type": "integer", "minimum": 0}


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def tool_definitions() -> List[Dict[str, Any]]:
    """``tools/list`` payload: exactly seven tools, in contract order."""
    tools = [
        {
            "name": "open_workspace",
            "title": "Open Autonom probe",
            "description": ("Open the Autonom probe page. Starts the local probe media endpoint when "
                            "needed and returns its state, the probe note and the current selection."),
            "inputSchema": EMPTY_INPUT,
            "annotations": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False},
            "_meta": {
                "ui": {"resourceUri": UI_URI},
                "openai/outputTemplate": UI_URI,
                "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
                "openai/widgetAccessible": True,
            },
        },
        {
            "name": "probe_read",
            "title": "Read Autonom probe status",
            "description": ("Read the probe status: host, server, Autonom CLI version, connected "
                            "devices, media endpoint, the probe note and the current selection."),
            "inputSchema": EMPTY_INPUT,
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "_meta": {"openai/widgetAccessible": True},
        },
        {
            "name": "probe_write",
            "title": "Write the Autonom probe note",
            "description": ("Replace the probe note (a harmless test value). Pass the note revision "
                            "from probe_read as expected_revision."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 500,
                             "description": "New note text, 1-500 characters, no control characters."},
                    "expected_revision": dict(REVISION, description="Current note revision."),
                    "selection_revision": dict(REVISION, description="Selection revision the write assumes."),
                },
                "required": ["text", "expected_revision"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                            "openWorldHint": False},
            "_meta": {"openai/widgetAccessible": True},
        },
        {
            "name": "probe_fail",
            "title": "Deliberate probe failure",
            "description": "Always fails, so the probe page can check how tool errors render.",
            "inputSchema": EMPTY_INPUT,
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "_meta": APP_ONLY_META,
        },
        {
            "name": "probe_select",
            "title": "Select a probe target",
            "description": "Store the probe page selection with a revision check.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_ref": {"type": "string", "pattern": TARGET_REF.pattern},
                    "expected_revision": REVISION,
                },
                "required": ["target_ref", "expected_revision"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                            "openWorldHint": False},
            "_meta": APP_ONLY_META,
        },
        {
            "name": "probe_media_frames",
            "title": "Fetch probe media frames",
            "description": "Fetch generated H.264 test frames from the probe media endpoint.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                    "after_seq": {"type": "integer", "minimum": -1, "default": -1},
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "_meta": APP_ONLY_META,
        },
        {
            "name": "probe_save_results",
            "title": "Save probe results",
            "description": "Save the results JSON of a probe run under the probe state directory.",
            "inputSchema": {
                "type": "object",
                "properties": {"results": {"type": "object"}},
                "required": ["results"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                            "openWorldHint": False},
            "_meta": APP_ONLY_META,
        },
    ]
    for tool in tools:
        tool["outputSchema"] = OUTPUT_SCHEMA
    return _copy(tools)


TOOL_NAMES = tuple(tool["name"] for tool in tool_definitions())


class UnknownTool(Exception):
    pass


def new_operation_id() -> str:
    return "op_" + secrets.token_hex(8)


def new_nonce() -> str:
    return "probe-%s-%s" % (secrets.token_hex(2), NONCE_WORDS[secrets.randbelow(len(NONCE_WORDS))])


def _text_dump(structured: Dict[str, Any]) -> str:
    """The sorted JSON text of a payload; escaped to ASCII when it holds a lone surrogate."""
    text = json.dumps(structured, ensure_ascii=False, sort_keys=True)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(structured, ensure_ascii=True, sort_keys=True)
    return text


def envelope(operation_id: str, fields: Dict[str, Any],
             meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    structured: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "operation_id": operation_id}
    structured.update(fields)
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": _text_dump(structured)}],
        "structuredContent": structured,
        "isError": structured.get("ok") is not True,
    }
    if meta is not None:
        result["_meta"] = {"autonom/probe": meta}
    return result


def failure(operation_id: str, exc: ProbeError,
            extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"ok": False}
    fields.update(exc.payload())
    for key, value in (extras or {}).items():
        fields.setdefault(key, value)
    return envelope(operation_id, fields)


def _bad(field: str, problem: str) -> ProbeError:
    return ProbeError("invalid_arguments", "Invalid argument %s: %s" % (field, problem),
                      field=field, problem=problem)


def _only(arguments: Dict[str, Any], allowed: Tuple[str, ...]) -> None:
    for key in arguments:
        if key not in allowed:
            raise _bad(str(key)[:80], "unknown argument")


def _int_arg(arguments: Dict[str, Any], key: str, minimum: int, maximum: Optional[int] = None,
             default: Optional[int] = None, required: bool = False) -> Optional[int]:
    if key not in arguments:
        if required:
            raise _bad(key, "required")
        return default
    value = arguments[key]
    if not is_int(value):
        raise _bad(key, "must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = ">= %d" % minimum if maximum is None else "%d-%d" % (minimum, maximum)
        raise _bad(key, "must be %s" % bound)
    return value


def host_info() -> Dict[str, Any]:
    return {"hostname": platform.node(), "platform": sys.platform,
            "os_release": platform.release(), "machine": platform.machine()}


def _int_or_float(value: float) -> Any:
    return int(value) if float(value).is_integer() else value


class ProbeTools:
    def __init__(self, resolver: Resolver, config: Dict[str, Any], store: StateStore,
                 media: MediaChild, server_info: Callable[[], Dict[str, Any]],
                 log: Callable[[str], None]) -> None:
        self.resolver = resolver
        self.config = config
        self.store = store
        self.media = media
        self.server_info = server_info
        self.log = log
        self.nonce = new_nonce()
        self.handlers: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {
            "open_workspace": self.open_workspace,
            "probe_read": self.probe_read,
            "probe_write": self.probe_write,
            "probe_fail": self.probe_fail,
            "probe_select": self.probe_select,
            "probe_media_frames": self.probe_media_frames,
            "probe_save_results": self.probe_save_results,
        }

    def call(self, name: str, arguments: Any) -> Dict[str, Any]:
        handler = self.handlers.get(name)
        if handler is None:
            raise UnknownTool(name)
        operation_id = new_operation_id()
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return failure(operation_id, _bad("arguments", "must be an object"))
        try:
            return handler(operation_id, arguments)
        except ProbeError as exc:
            return failure(operation_id, exc)

    # -- shared pieces

    def local_origin(self) -> str:
        return "http://127.0.0.1:%d" % self.media.effective_port()

    def _media_summary(self) -> Dict[str, Any]:
        state = self.media.status()
        failed = self.media.failure if state == "failed" else None
        summary: Dict[str, Any] = {
            "state": state,
            "local_origin": self.local_origin(),
            "port": self.media.effective_port(),
            "remote_routes": [dict(route) for route in self.config.get("remote_routes") or []],
            "allowed_devices": list(self.config.get("allowed_devices") or []),
            "error_code": failed.error_code if failed else None,
            "error": failed.error if failed else None,
            "hint": failed.hint if failed else None,
        }
        if failed is not None and isinstance(failed.extras.get("reason"), str):
            summary["reason"] = failed.extras["reason"]
        if state == "running" and self.media.shared:
            summary["shared"] = True
        return summary

    def _selection(self) -> Dict[str, Any]:
        selection = self.store.read_selection()
        return {"target_ref": selection["target_ref"], "revision": selection["revision"]}

    # -- open_workspace

    def open_workspace(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ())
        self.media.ensure()
        fields = {
            "ok": True,
            "probe_version": PROBE_VERSION,
            "media": self._media_summary(),
            "selection": self._selection(),
            "note": {"revision": self.store.read_note()["revision"]},
        }
        meta = {"media_token": self.media.current_token(), "context_nonce": self.nonce}
        return envelope(operation_id, fields, meta)

    # -- probe_read

    def _run_cli(self, python: str, cli: str, args: List[str]) -> Dict[str, Any]:
        display = ["autonom"] + args
        timeout = float(self.config["cli_timeout_s"])
        label = " ".join(display)
        try:
            proc = subprocess.Popen(
                [python, cli] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
                env=child_env(self.resolver.env, self.resolver.home), close_fds=True)
        except OSError as exc:
            raise ProbeError("cli_failed", "`%s` could not be started (%s)" % (label, type(exc).__name__),
                             command=display, exit_code=None)
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        with contextlib.suppress(OSError, ValueError):
                            stream.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            raise ProbeError("cli_timeout", "`%s` did not finish within %s s" % (label, _int_or_float(timeout)),
                             timeout_s=_int_or_float(timeout), command=display)
        text = out.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise ProbeError("cli_failed", "`%s` did not print a JSON object" % label,
                             command=display, exit_code=proc.returncode)
        if data.get("ok") is False or proc.returncode != 0:
            extras: Dict[str, Any] = {"command": display, "exit_code": proc.returncode}
            for source, target in (("error_code", "cli_error_code"), ("error", "cli_error"),
                                   ("hint", "cli_hint")):
                if isinstance(data.get(source), str):
                    extras[target] = data[source][:500]
            hint = data.get("hint") if isinstance(data.get("hint"), str) and data.get("hint") else None
            raise ProbeError("cli_failed", "`%s` failed" % label, hint=hint, **extras)
        return data

    def _media_status(self) -> Dict[str, Any]:
        state = self.media.status()
        failed = self.media.failure if state == "failed" else None
        summary: Dict[str, Any] = {"state": state, "port": self.media.effective_port(), "clients": None,
                                   "refused_origins": [],
                                   "error_code": failed.error_code if failed else None}
        if state == "running" and self.media.shared:
            summary["shared"] = True
        if state != "running":
            return summary
        try:
            status, body = self.media.get("/probe/status", 1.0)
        except (ProbeError, OSError, ValueError, http.client.HTTPException):
            return summary
        if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            return summary
        if is_int(body.get("clients")):
            summary["clients"] = body["clients"]
        refused: List[Dict[str, Any]] = []
        for item in body.get("refused_origins") or []:
            if not isinstance(item, dict) or not isinstance(item.get("origin"), str):
                continue
            refused.append({
                "origin": item["origin"][:200],
                "count": item["count"] if is_int(item.get("count")) else None,
                "last_at": item["last_at"] if isinstance(item.get("last_at"), str) else None,
            })
            if len(refused) >= 20:
                break
        summary["refused_origins"] = refused
        return summary

    def probe_read(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ())
        host = host_info()
        server = self.server_info()
        try:
            cli = self.resolver.resolve_cli()
            python, python_version = self.resolver.resolve_cli_python()
            version_data = self._run_cli(python, cli, ["version"])
            if not isinstance(version_data.get("version"), str):
                raise ProbeError("cli_failed", "`autonom version` printed no version",
                                 command=["autonom", "version"], exit_code=0)
            devices_data = self._run_cli(python, cli, ["devices", "list"])
            raw_devices = devices_data.get("devices")
            if not isinstance(raw_devices, list):
                raise ProbeError("cli_failed", "`autonom devices list` printed no devices list",
                                 command=["autonom", "devices", "list"], exit_code=0)
        except ProbeError as exc:
            return failure(operation_id, exc, {"host": host, "server": server})
        devices: List[Dict[str, Any]] = []
        for entry in raw_devices[:MAX_DEVICES]:
            if isinstance(entry, dict):
                devices.append({key: entry[key] for key in DEVICE_KEYS if key in entry})
        warnings = devices_data.get("warnings")
        note = self.store.read_note()
        fields = {
            "ok": True,
            "host": host,
            "server": server,
            "autonom": {"version": version_data["version"][:80], "cli": "autonom",
                        "cli_python": python_version},
            "devices": devices,
            "devices_warnings": len(warnings) if isinstance(warnings, list) else 0,
            "media": self._media_status(),
            "note": {"revision": note["revision"], "text": note["text"], "updated_at": note["updated_at"]},
            "selection": self._selection(),
        }
        return envelope(operation_id, fields)

    # -- probe_write / probe_select / probe_fail

    def probe_write(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ("text", "expected_revision", "selection_revision"))
        if "text" not in arguments:
            raise _bad("text", "required")
        text = arguments["text"]
        if not isinstance(text, str):
            raise _bad("text", "must be a string")
        if not 1 <= len(text) <= 500:
            raise _bad("text", "must be 1-500 characters")
        if CONTROL.search(text):
            raise _bad("text", "must not contain control characters")
        expected = _int_arg(arguments, "expected_revision", 0, required=True)
        selection_revision = _int_arg(arguments, "selection_revision", 0)
        result = self.store.write_note(text, int(expected or 0), selection_revision, operation_id)
        fields: Dict[str, Any] = {"ok": True}
        fields.update(result)
        return envelope(operation_id, fields)

    def probe_select(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ("target_ref", "expected_revision"))
        if "target_ref" not in arguments:
            raise _bad("target_ref", "required")
        target_ref = arguments["target_ref"]
        if not isinstance(target_ref, str) or not TARGET_REF.fullmatch(target_ref):
            raise _bad("target_ref", "must match %s" % TARGET_REF.pattern)
        expected = _int_arg(arguments, "expected_revision", 0, required=True)
        selection = self.store.select(target_ref, int(expected or 0))
        return envelope(operation_id, {"ok": True, "selection": selection})

    def probe_fail(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ())
        raise ProbeError("probe_deliberate_failure", "Deliberate failure for the bridge.error check",
                         probe=True, detail={"nested": [1, "two", None]})

    # -- probe_media_frames

    def probe_media_frames(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ("count", "after_seq"))
        count = _int_arg(arguments, "count", 1, 30, default=10)
        after_seq = _int_arg(arguments, "after_seq", -1, default=-1)
        state = self.media.status()
        if state == "failed" and self.media.failure is not None:
            last = self.media.failure
            extras: Dict[str, Any] = {"media_error_code": last.error_code}
            if isinstance(last.extras.get("reason"), str):
                extras["reason"] = last.extras["reason"]
            raise ProbeError("media_start_failed", "The probe media endpoint failed to start: %s"
                             % last.error, **extras)
        if state != "running":
            raise ProbeError("media_not_running", "The probe media endpoint is not running",
                             media_state=state)
        try:
            status, body = self.media.get("/probe/frames?count=%d&after_seq=%d" % (count, after_seq), 2.0)
        except ProbeError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ProbeError("media_not_running", "The probe media endpoint did not answer",
                             media_state=self.media.status(), reason=type(exc).__name__)
        if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            code = body.get("error_code") if isinstance(body, dict) else None
            raise ProbeError("media_not_running", "The probe media endpoint refused the frames request",
                             media_state=self.media.status(), http_status=status,
                             media_error_code=code if isinstance(code, str) else None)
        frames_out: List[Dict[str, Any]] = []
        total = 0
        last_seq = after_seq
        for frame in body.get("frames") or []:
            if not isinstance(frame, dict) or not is_int(frame.get("seq")):
                continue
            data = frame.get("data_b64") if isinstance(frame.get("data_b64"), str) else ""
            try:
                total += len(base64.b64decode(data, validate=True))
            except (binascii.Error, ValueError):
                continue
            t_ms = frame.get("t_server_ms")
            frames_out.append({"seq": frame["seq"], "key": frame.get("key") is True,
                               "t_server_ms": t_ms if is_number(t_ms) else None, "data_b64": data})
            last_seq = frame["seq"]
        fields = {
            "ok": True,
            "frames": len(frames_out),
            "bytes": total,
            "last_seq": last_seq,
            "codec": body.get("codec") if isinstance(body.get("codec"), str) else None,
            "width": body.get("width") if is_int(body.get("width")) else None,
            "height": body.get("height") if is_int(body.get("height")) else None,
        }
        config_b64 = body.get("config_b64") if isinstance(body.get("config_b64"), str) else None
        return envelope(operation_id, fields, {"config_b64": config_b64, "frames": frames_out})

    # -- probe_save_results

    def probe_save_results(self, operation_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        _only(arguments, ("results",))
        if "results" not in arguments:
            raise _bad("results", "required")
        results = arguments["results"]
        if not isinstance(results, dict):
            raise _bad("results", "must be an object")
        try:
            compact = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
            size = len(compact.encode("utf-8"))
        except UnicodeEncodeError:
            raise ProbeError("results_invalid", "results must be valid UTF-8 text (no lone surrogates)",
                             field="results", problem="must be valid UTF-8 text")
        except (ValueError, RecursionError):
            raise ProbeError("results_invalid", "results must be plain JSON that is not nested too deeply",
                             field="results", problem="must be plain JSON")
        if size > MAX_RESULTS_BYTES:
            raise ProbeError("results_too_large", "The results JSON is %d bytes, over the 512 KiB limit" % size,
                             bytes=size, limit_bytes=MAX_RESULTS_BYTES)
        schema = results.get("schema_version")
        if not is_int(schema) or schema != 1:
            raise ProbeError("results_invalid", "results.schema_version must be 1",
                             field="results.schema_version", problem="must be 1")
        run_id = results.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ProbeError("results_invalid", "results.run_id must match %s" % RUN_ID.pattern,
                             field="results.run_id", problem="must match %s" % RUN_ID.pattern)
        if not isinstance(results.get("checks"), list):
            raise ProbeError("results_invalid", "results.checks must be a list",
                             field="results.checks", problem="must be a list")
        token = self.media.current_token()
        if (token and token in compact) or "bearer." in compact.lower():
            raise ProbeError("results_contain_secret",
                             "The results contain the media token or a bearer subprotocol value")
        written = self.store.save_results(run_id, json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        return envelope(operation_id, {"ok": True, "saved": {
            "run_id": run_id, "path_hint": "chatgpt-probe/results/%s.json" % run_id, "bytes": written}})
