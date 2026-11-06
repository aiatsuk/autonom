"""The 28-tool catalog in contract order (contract 2.9).

Each tool module exports ``TOOLS`` (a tuple of ``mcp_common.Tool``) and may
define ``setup(services)``, called once at server start. ``build_catalog``
checks that the union of the four modules is exactly the 28 names below
(startup assertion). Until every module is filled in, the core build lists
only its own tools, and that is accepted only when
``AUTONOM_MCP_PARTIAL_CATALOG=1``. Test-only tools are appended after the
catalog when ``AUTONOM_MCP_TEST_BAD_OUTPUT=1``.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Tuple

import mcp_core_tools
import mcp_media
import mcp_runs
import mcp_sessions
from mcp_common import Tool, output_schema, session_scoped

TOOL_ORDER: Tuple[str, ...] = (
    "list_hosts", "open_workspace", "save_diagnostics",
    "list_devices", "list_sessions", "get_session", "start_session", "stop_session",
    "get_screen", "get_ui_tree", "tap_element", "type_text", "press_key",
    "acquire_control", "release_control", "get_operation",
    "list_flows", "start_flow_run", "get_run", "cancel_run", "list_artifacts", "get_artifact",
    "open_stream", "get_frames", "close_stream", "send_input", "pause_session", "resume_session",
)
OWNERS: Dict[str, Any] = {
    "mcp_core_tools": mcp_core_tools,
    "mcp_sessions": mcp_sessions,
    "mcp_runs": mcp_runs,
    "mcp_media": mcp_media,
}
OWNER_OF: Dict[str, str] = {}
for _index, _name in enumerate(TOOL_ORDER):
    OWNER_OF[_name] = ("mcp_core_tools" if _index < 3 else "mcp_sessions" if _index < 16
                       else "mcp_runs" if _index < 22 else "mcp_media")
PARTIAL_ENV = "AUTONOM_MCP_PARTIAL_CATALOG"
APP_ONLY = ("save_diagnostics", "open_stream", "get_frames", "close_stream", "send_input",
            "pause_session", "resume_session")


class CatalogError(Exception):
    pass


def partial_mode(env: Mapping[str, str]) -> bool:
    return str(env.get(PARTIAL_ENV, "")) == "1"


def build_catalog(env: Mapping[str, str]) -> List[Tool]:
    """The tools in contract order; raises ``CatalogError`` when the union is wrong."""
    by_name: Dict[str, Tool] = {}
    for module_name, module in OWNERS.items():
        for tool in getattr(module, "TOOLS", ()):
            if tool.name in by_name:
                raise CatalogError("tool %s is defined twice" % tool.name)
            if tool.name not in OWNER_OF:
                raise CatalogError("tool %s is not in the catalog" % tool.name)
            if OWNER_OF[tool.name] != module_name:
                raise CatalogError("tool %s belongs to %s, not %s" % (tool.name, OWNER_OF[tool.name],
                                                                     module_name))
            by_name[tool.name] = tool
    missing = [name for name in TOOL_ORDER if name not in by_name]
    if missing and not partial_mode(env):
        raise CatalogError("the catalog lacks %d tools (%s); set %s=1 only for a partial build"
                           % (len(missing), ", ".join(missing), PARTIAL_ENV))
    tools = [by_name[name] for name in TOOL_ORDER if name in by_name]
    if mcp_core_tools.test_mode(env):
        tools.extend(mcp_core_tools.TEST_TOOLS)
    return tools


def definition(tool: Tool) -> Dict[str, Any]:
    """The ``tools/list`` entry of one tool."""
    meta = copy.deepcopy(tool.meta) if tool.meta else {}
    meta.setdefault("openai/widgetAccessible", True)
    annotations = dict(tool.annotations)
    annotations.setdefault("openWorldHint", False)
    return {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": copy.deepcopy(tool.input_schema),
        "outputSchema": output_schema(tool.name, copy.deepcopy(tool.output_fields), session_scoped(tool)),
        "annotations": annotations,
        "_meta": meta,
    }


def setup_modules(services: Any) -> None:
    for module in OWNERS.values():
        fn = getattr(module, "setup", None)
        if callable(fn):
            fn(services)
