"""Core tools: ``list_hosts``, ``open_workspace``, ``save_diagnostics`` and the UI resource.

Contract sections 2.11-2.13. With ``AUTONOM_MCP_TEST_BAD_OUTPUT=1`` the
module also offers test-only tools (``TEST_TOOLS``): a tool that breaks its
own output schema, a slow mutation that holds the session guard, a fake run
that holds it until a session hook lets go, and recording session hooks.
"""
from __future__ import annotations

import json
import os
import platform as stdlib_platform
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_common import (ADAPTER_VERSION, DIAGNOSTICS_RUN_ID_RE, EMPTY_INPUT, HOST_ID, SCHEMA_VERSION,
                        SESSION_ENTRY, SESSION_ID, CallContext, Reply, Services, Tool, ToolError,
                        integer, known_secrets, new_id, nullable, obj, rejected, string)
from mcp_env import ensure_private_dir

UI_URI = "ui://autonom/workspace-dev.html"
UI_MIME = "text/html;profile=mcp-app"
CORE_MARKER = "<!-- WORKSPACE_CORE -->"
CONFIG_MARKER = "__WORKSPACE_CONFIG_JSON__"
MAX_DIAGNOSTICS = 256 * 1024
TEST_ENV = "AUTONOM_MCP_TEST_BAD_OUTPUT"
_DIAG_RUN = re.compile(DIAGNOSTICS_RUN_ID_RE)

READ = {"readOnlyHint": True, "openWorldHint": False}
WIDGET = {"openai/widgetAccessible": True}
APP_ONLY = {"ui": {"visibility": ["app"]}, "openai/visibility": "private", "openai/widgetAccessible": True}
INPUT_KINDS = ("tap", "swipe", "key", "text", "system")

HOST_ENTRY = obj({
    "host_id": nullable(HOST_ID),
    "name": string(max_length=80),
    "kind": {"const": "local"},
    "version": nullable(string(max_length=40)),
    "state": string(enum=("online", "error")),
    "capabilities": obj({
        "sessions": {"type": "boolean"},
        "control": {"type": "boolean"},
        "flows": {"type": "boolean"},
        "artifacts": {"type": "boolean"},
        "media": obj({"transport": {"const": "bridge"}, "codec": {"const": "h264"}},
                     ("transport", "codec")),
        "input": {"type": "array", "maxItems": 8, "items": string(enum=INPUT_KINDS)},
    }, ("sessions", "control", "flows", "artifacts", "media", "input")),
    "error_code": string(max_length=80),
    "error": string(max_length=500),
}, ("host_id", "name", "kind", "version", "state", "capabilities"))
HOSTS = {"type": "array", "maxItems": 16, "items": HOST_ENTRY}


# ---------------------------------------------------------------- list_hosts

def local_host_entries(services: Services, host_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """The host list of 2.11: the local host, ``online`` or ``error`` (never raises)."""
    capabilities = {
        "sessions": True, "control": True, "flows": bool(services.config.get("flow_roots")),
        "artifacts": True, "media": {"transport": "bridge", "codec": "h264"},
        "input": list(INPUT_KINDS),
    }
    try:
        info = services.refs.local_host()
    except ToolError as exc:
        reason = exc.extras.get("cli_error_code")
        message = exc.error if not reason else "%s (CLI code %s)" % (exc.error, reason)
        return [{"host_id": None, "name": (stdlib_platform.node() or "localhost")[:80], "kind": "local",
                 "version": None, "state": "error", "capabilities": capabilities,
                 "error_code": exc.error_code, "error": message[:500]}]
    if host_filter is not None and host_filter != info["host_id"]:
        return []
    return [{"host_id": info["host_id"], "name": info["name"], "kind": "local",
             "version": info.get("autonom_version"), "state": "online", "capabilities": capabilities}]


def _config_warnings(ctx: CallContext) -> None:
    for item in getattr(ctx.services.config, "warnings", []) or []:
        ctx.warn(item.get("code", "config_error"), item.get("message", ""))


def list_hosts(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _config_warnings(ctx)
    hosts = local_host_entries(ctx.services, args.get("host_id"))
    ctx.host_id = ctx.services.refs.cached_host_id()
    return {"hosts": hosts}


# ---------------------------------------------------------------- open_workspace

WORKSPACE = obj({
    "adapter_version": {"const": ADAPTER_VERSION},
    "host_id": nullable(HOST_ID),
    "hosts": HOSTS,
    "sessions": {"type": "array", "maxItems": 256, "items": SESSION_ENTRY},
}, ("adapter_version", "host_id", "hosts", "sessions"))


def open_workspace(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _config_warnings(ctx)
    hosts = local_host_entries(ctx.services)
    host_id = ctx.services.refs.cached_host_id()
    ctx.host_id = host_id
    sessions: List[Dict[str, Any]] = []
    if host_id is None:
        ctx.warn("sessions_unavailable", "The local host identity is unavailable, so no sessions are listed")
    else:
        try:
            sessions = [entry for entry in ctx.services.refs.sessions() if entry["live"]][:256]
        except ToolError as exc:
            ctx.warn("sessions_unavailable", "The session list failed: %s" % exc.error_code)
    return {"workspace": {"adapter_version": ADAPTER_VERSION, "host_id": host_id, "hosts": hosts,
                          "sessions": sessions}}


# ---------------------------------------------------------------- save_diagnostics

def _contains_secret(text: str, home: str) -> bool:
    lowered = text.lower()
    if "bearer " in lowered or "token=" in lowered:
        return True
    if home and len(home) > 1 and home in text:
        return True
    return any(secret in text for secret in known_secrets())


def write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with mode 0600."""
    ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_diagnostics(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.host_id = ctx.services.refs.cached_host_id()
    results = args["results"]
    try:
        text = json.dumps(results, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        data = text.encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeEncodeError):
        raise rejected("diagnostics_invalid", "The results cannot be stored as JSON")
    if len(data) > MAX_DIAGNOSTICS:
        raise rejected("diagnostics_too_large", "The results are %d bytes; at most %d are kept"
                       % (len(data), MAX_DIAGNOSTICS))
    version = results.get("schema_version")
    if not (isinstance(version, int) and not isinstance(version, bool) and version == 1):
        raise rejected("diagnostics_invalid", "results.schema_version must be 1")
    run_id = results.get("run_id")
    if not (isinstance(run_id, str) and _DIAG_RUN.fullmatch(run_id)):
        raise rejected("diagnostics_invalid", "results.run_id must look like diag-YYYYMMDD-HHMMSS-xxxx")
    if _contains_secret(text, str(ctx.services.home)):
        raise rejected("diagnostics_contain_secret",
                       "The results contain a token, a bearer value or a home path")
    path = Path(ctx.services.state_dir) / "diagnostics" / ("%s.json" % run_id)
    try:
        ensure_private_dir(Path(ctx.services.state_dir))  # the MCP state dir itself is 0700 too
        write_private(path, data + b"\n")
    except OSError as exc:
        raise ToolError("write_failed", "The diagnostics file could not be written: %s" % type(exc).__name__)
    return {"saved": {"run_id": run_id, "bytes": len(data) + 1}}


# ---------------------------------------------------------------- resource

class ResourceNotFound(Exception):
    pass


class ResourceBroken(Exception):
    pass


def resource_meta() -> Dict[str, Any]:
    return {
        "ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
               "prefersBorder": False},
        "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]},
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
        "openai/widgetDescription": ("Autonom development workspace: sessions, live device video "
                                     "through the MCP bridge, input, control and diagnostics."),
    }


def resource_entries(services: Services) -> List[Dict[str, Any]]:
    return [{"uri": UI_URI, "name": "autonom-workspace-dev", "title": "Autonom (dev)",
             "mimeType": UI_MIME, "_meta": resource_meta()}]


def assemble_html(ui_dir: Path) -> str:
    try:
        page = (Path(ui_dir) / "workspace-dev.html").read_text(encoding="utf-8")
        core = (Path(ui_dir) / "workspace-dev-core.mjs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ResourceBroken("The workspace UI files are missing")
    if page.count(CORE_MARKER) != 1 or page.count(CONFIG_MARKER) != 1:
        raise ResourceBroken("The workspace UI page markers are missing")
    if not re.search(r"<script\b[^>]*\bid=\"workspace-config\"[^>]*>[^<]*" + CONFIG_MARKER, page):
        raise ResourceBroken("The workspace UI config element is missing")
    if re.search(r"</script", core, re.IGNORECASE):
        raise ResourceBroken("The workspace UI core cannot be inlined")
    config = json.dumps({"adapter_version": ADAPTER_VERSION, "schema_version": SCHEMA_VERSION})
    page = page.replace(CONFIG_MARKER, config.replace("</", "<\\/"))
    return page.replace(CORE_MARKER, '<script type="module">\n' + core + "\n</script>")


def read_resource(services: Services, uri: str) -> Dict[str, Any]:
    if uri != UI_URI:
        raise ResourceNotFound(uri)
    html = assemble_html(services.resolver.ui_dir())
    return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": html, "_meta": resource_meta()}]}


# ---------------------------------------------------------------- catalog entries

TOOLS = (
    Tool(
        name="list_hosts",
        title="List Autonom hosts",
        description=("List the Autonom hosts this adapter can drive (the local machine for now) with "
                     "their host_id, version, state and capabilities. Pass host_id to filter."),
        input_schema=obj({"host_id": HOST_ID}),
        output_fields={"hosts": HOSTS},
        annotations=dict(READ),
        meta=dict(WIDGET),
        mutating=False,
        handler=list_hosts,
    ),
    Tool(
        name="open_workspace",
        title="Open Autonom",
        description=("Open the Autonom development workspace page. Returns the adapter version, the "
                     "hosts and the live sessions. Device tools need host_id and session_id from "
                     "list_sessions or start_session."),
        input_schema=dict(EMPTY_INPUT),
        output_fields={"workspace": WORKSPACE},
        annotations=dict(READ),
        meta={"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI,
              "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
              "openai/widgetAccessible": True},
        mutating=False,
        handler=open_workspace,
    ),
    Tool(
        name="save_diagnostics",
        title="Save workspace diagnostics",
        description=("Save the results of the diagnostics page (schema_version 1, run_id diag-...) "
                     "under the adapter state directory. Results with tokens or home paths are refused."),
        input_schema=obj({"results": {"type": "object", "description": "Diagnostics results object."}},
                         ("results",)),
        output_fields={"saved": obj({"run_id": string(DIAGNOSTICS_RUN_ID_RE), "bytes": integer(minimum=0)},
                                    ("run_id", "bytes"))},
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False},
        meta=dict(APP_ONLY),
        mutating=False,
        handler=save_diagnostics,
    ),
)


# ---------------------------------------------------------------- test-only seam

def _seam(services: Services) -> Dict[str, Any]:
    return services.extensions.setdefault("test_seam", {"calls": [], "runs": {}, "lock": threading.Lock()})


def _test_bad_output(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"undeclared_field": True}


def _test_slow_mutation(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"])
    with ctx.services.guard.mutation(ref["session_id"], ctx.tool.name):
        time.sleep(args.get("ms", 0) / 1000.0)
    return {"held_ms": args.get("ms", 0)}


def _test_hold_run(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"])
    session_id = ref["session_id"]
    run_id = new_id("run_", 8)
    ctx.services.guard.hold(session_id, {"run_id": run_id})
    done = threading.Event()
    seam = _seam(ctx.services)
    with seam["lock"]:
        seam["runs"][session_id] = (run_id, done)

    def holder() -> None:
        done.wait(args.get("ms", 30000) / 1000.0)
        ctx.services.guard.release(session_id)
        with seam["lock"]:
            seam["runs"].pop(session_id, None)

    threading.Thread(target=holder, daemon=True).start()
    return {"run_id": run_id}


def _test_hooks(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    seam = _seam(ctx.services)
    if args["action"] == "register":
        for name in args.get("names") or []:
            def hook(session_id: str, _name: str = name) -> str:
                with seam["lock"]:
                    seam["calls"].append({"name": _name, "session_id": session_id})
                return "recorded"
            ctx.services.hooks.register(name, hook)
    with seam["lock"]:
        calls = [dict(item) for item in seam["calls"]]
    return {"calls": calls}


def _release_test_run(services: Services, session_id: str) -> Optional[str]:
    seam = _seam(services)
    with seam["lock"]:
        entry = seam["runs"].get(session_id)
    if entry is None:
        return None
    entry[1].set()
    return "released %s" % entry[0]


REF_INPUT = {"host_id": HOST_ID, "session_id": SESSION_ID}
TEST_MUTATION = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
TEST_TOOLS = (
    Tool("test_bad_output", "Test: invalid output", "Test-only: returns a field its schema does not declare.",
         dict(EMPTY_INPUT), {}, dict(READ), dict(APP_ONLY), False, _test_bad_output),
    Tool("test_bad_mutation", "Test: invalid mutation output",
         "Test-only: a mutating tool whose output breaks its schema.",
         dict(EMPTY_INPUT), {}, dict(TEST_MUTATION), dict(APP_ONLY), True, _test_bad_output),
    Tool("test_slow_mutation", "Test: slow mutation",
         "Test-only: holds the session guard as a mutation for ms milliseconds.",
         obj(dict(REF_INPUT, ms=integer(0, 60000)), ("host_id", "session_id")),
         {"held_ms": integer(minimum=0)}, dict(TEST_MUTATION), dict(APP_ONLY), True, _test_slow_mutation),
    Tool("test_hold_run", "Test: hold a fake run",
         "Test-only: holds the session guard as a run until the stop hook or ms milliseconds.",
         obj(dict(REF_INPUT, ms=integer(0, 120000)), ("host_id", "session_id")),
         {"run_id": string(r"^run_[0-9a-f]{16}$")}, dict(TEST_MUTATION), dict(APP_ONLY), True, _test_hold_run),
    Tool("test_hooks", "Test: session hooks",
         "Test-only: registers recording session hooks, or lists the recorded calls.",
         obj({"action": string(enum=("register", "calls")),
              "names": {"type": "array", "maxItems": 8, "items": string(max_length=40)}}, ("action",)),
         {"calls": {"type": "array", "items": obj({"name": string(), "session_id": string()},
                                                  ("name", "session_id"))}},
         dict(TEST_MUTATION), dict(APP_ONLY), False, _test_hooks),
)


def test_mode(env: Any) -> bool:
    return str(env.get(TEST_ENV, "")) == "1"


def setup(services: Services) -> None:
    if test_mode(services.env):
        services.hooks.register("test_run", lambda session_id: _release_test_run(services, session_id))
