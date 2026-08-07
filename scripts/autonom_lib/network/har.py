"""HAR 1.2 export (CAP-NET-006).

The export is honest about its own fidelity: when the session ran without
`--capture-bodies`, `response.content.text` holds a truncated preview while
`content.size` reports the true byte count, and `log.comment` says so. A HAR that
silently presented a 2 KiB preview as the whole payload would let a reader
conclude the app sent a truncated body.
"""
from __future__ import annotations

from typing import Any

from .. import __version__

HAR_VERSION = "1.2"
PREVIEW_NOTE = (
    "Response and request content are truncated previews, not full bodies: this "
    "session was captured without --capture-bodies. 'size' reports the true byte "
    "count; 'text' does not."
)


def _headers(mapping: dict[str, str] | None) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in sorted((mapping or {}).items())]


def _query(url: str) -> list[dict[str, str]]:
    if "?" not in (url or ""):
        return []
    from urllib.parse import parse_qsl

    return [{"name": name, "value": value}
            for name, value in parse_qsl(url.split("?", 1)[1], keep_blank_values=True)]


def _mime(headers: dict[str, str] | None) -> str:
    return (headers or {}).get("content-type", "").split(";")[0] or "application/octet-stream"


def entry_for(flow: dict[str, Any]) -> dict[str, Any]:
    request_headers = flow.get("request_headers_preview") or {}
    response_headers = flow.get("response_headers_preview") or {}
    sizes = flow.get("sizes") or {}
    duration = flow.get("duration_ms") or 0

    entry: dict[str, Any] = {
        "startedDateTime": flow.get("started_at"),
        "time": duration,
        "request": {
            "method": flow.get("method"),
            "url": flow.get("url"),
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": _headers(request_headers),
            "queryString": _query(flow.get("url") or ""),
            "headersSize": -1,
            "bodySize": sizes.get("request_bytes", 0),
        },
        "response": {
            "status": flow.get("status"),
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": _headers(response_headers),
            "content": {
                "size": sizes.get("response_bytes", 0),
                "mimeType": _mime(response_headers),
                "text": flow.get("response_body_preview") or "",
            },
            "redirectURL": response_headers.get("location", ""),
            "headersSize": -1,
            "bodySize": sizes.get("response_bytes", 0),
        },
        "cache": {},
        "timings": {"send": 0, "wait": duration, "receive": 0},
        "_autonom": {"id": flow.get("id"), "mocked": bool(flow.get("mocked")),
                     "mock_id": flow.get("mock_id")},
    }
    body = flow.get("request_body_preview")
    if body:
        entry["request"]["postData"] = {
            "mimeType": _mime(request_headers),
            "text": body,
            "params": [],
        }
    return entry


def build(flows: list[dict[str, Any]], *, bodies_captured: bool = False) -> dict[str, Any]:
    log: dict[str, Any] = {
        "version": HAR_VERSION,
        "creator": {"name": "autonom", "version": __version__},
        "pages": [],
        "entries": [entry_for(flow) for flow in flows],
    }
    if not bodies_captured:
        log["comment"] = PREVIEW_NOTE
    return {"log": log}
