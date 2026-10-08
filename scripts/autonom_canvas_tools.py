#!/usr/bin/env python3
"""Persistent NDJSON tools process behind the Mobile Canvas Tools drawer.

The drawer's panels (permissions, location, simulations, network capture and
mocks) reach the device only through this process. It is separate from the
input bridge (`autonom_canvas_bridge.py`) so that slow panel work, such as a
network attach, never delays a tap or the stream.

Protocol: one JSON request per line on stdin, one reply per line on stdout,
in order.

    {"id": 1, "op": "location.set", "payload": {...}, "origin": "human"}
    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error_code": "...", "error": "...", "hint": null,
     "capability": null}

One bad request never stops the process, and stdout carries protocol lines
only: at start the real stdout is moved to a private descriptor and file
descriptor 1 points at stderr, so nothing a library or a child process prints
can corrupt a reply.

Every operation uses the same `autonom_lib` functions (and the same shared
`network ... ` helpers in `autonom.py`) as the CLI. The target is the
Canvas's own; operations that need an Autonom session use the current one
only when it is on this target, the rule `autonom_canvas_bridge` journals by.
Every mutating operation is journaled there with its origin, never with a mock
body, push payload text or log text (lengths only).

The device actions of the Canvas Actions drawer (screenshot, recording,
install, launch, open URL, app language), the captures gallery and tool health
are ops of this process too (`ACTION_*_OPS`). Captures go to the captures store
(`autonom_lib.captures`); installs read only inside `--install-root` folders.
At most one recording runs per device; when the process ends (stdin closed or
SIGTERM) a running recording is stopped, its device file and partial file are
removed, and nothing is saved.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autonom as cli  # noqa: E402  (the shared network helpers)
import autonom_canvas_bridge as bridge  # noqa: E402
from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import captures, tool_health  # noqa: E402
from autonom_lib import device_state, errors, ios_simctl, journal  # noqa: E402
from autonom_lib import screenshot, session, simulator  # noqa: E402
from autonom_lib.network import device_proxy_ios  # noqa: E402
from autonom_lib.network import mocks as mocks_mod  # noqa: E402
from autonom_lib.network import redact, store  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

ORIGINS = bridge.ORIGINS
READ_OPS = ("context", "permissions.list", "location.get", "network.status",
            "network.requests", "network.request", "mocks.list")
MUTATING_OPS = ("permissions.set", "location.set", "location.clear", "simulate",
                "network.start", "network.attach", "network.detach", "network.stop",
                "mocks.add", "mocks.update", "mocks.enable", "mocks.disable",
                "mocks.remove", "mocks.clear")
# The Actions drawer (contract section 5.1): separate tuples, so the Tools lists stay pinned.
ACTION_READ_OPS = ("captures.list", "record.status", "apps.candidates", "health")
ACTION_DEVICE_OPS = ("capture.screenshot", "record.start", "record.stop", "app.install",
                     "app.launch", "app.open_url", "app.locale")
ACTION_LOCAL_OPS = ("captures.delete",)     # writes, but not a device mutation
INTERNAL_OPS = ("captures.file",)           # only for the Node file route
OPS = (READ_OPS + MUTATING_OPS + ACTION_READ_OPS + ACTION_DEVICE_OPS + ACTION_LOCAL_OPS
       + INTERNAL_OPS)
# Ops answered without the journal: reads, the gallery's own deletes, the file lookup.
UNJOURNALED_OPS = READ_OPS + ACTION_READ_OPS + ACTION_LOCAL_OPS + INTERNAL_OPS

RECORD_LIMIT_S = 180
RECORD_STOP_WAIT_S = 10.0
RECORD_SETTLE_S = 1.0
RECORD_PULL_TIMEOUT_S = 120
CLEANUP_TIMEOUT_S = 5
# The whole exit cleanup fits well inside the Node side's TOOLS_CLOSE_GRACE_MS (6 s).
SHUTDOWN_BUDGET_S = 4.0
INSTALL_TIMEOUT_S = 180
MAX_INSTALL_PATH = 4096
CANDIDATE_DEPTH = 7
CANDIDATE_MAX_ENTRIES = 5000
CANDIDATE_MAX_SECONDS = 2.0
CANDIDATE_COUNT = 20
CANDIDATE_SKIP = ("node_modules", "Pods")
INSTALL_HINT = ("Start the Canvas with --install-root <folder>, for example "
                "`autonom canvas --install-root ~/src/app/build`")
ACTION_APP_ID = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_-]+)+")
URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
LOCALE = re.compile(r"(?P<lang>[a-z]{2,3}(-[A-Z][a-z]{3})?)(-(?P<region>[A-Z]{2}|[0-9]{3}))?")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
INSTALL_REASON = re.compile(r"\b(INSTALL_[A-Z_]+)\b")
APP_LOCALES_MIN_SDK = 33

# The device simulations the drawer may run, and the values each one takes.
SIMULATE_VALUES: dict[str, tuple[str, ...]] = {
    "push": ("app_id", "payload"),
    "biometric": (),
    "battery": ("level", "state"),
    "network": ("speed", "delay"),
    "appearance": (),
}
SIMULATE_CONTROLS = tuple(SIMULATE_VALUES)
# What `context.simulate` offers on each platform (the library refuses the rest).
SIMULATE_SUPPORT: dict[str, dict[str, tuple[str, ...]]] = {
    IOS: {"push": ("send",),
          "biometric": simulator.CONTROL_ACTIONS["biometric"],
          "battery": simulator.CONTROL_ACTIONS["battery"],
          "appearance": simulator.CONTROL_ACTIONS["appearance"]},
    ANDROID: {"biometric": ("match",),
              "battery": simulator.CONTROL_ACTIONS["battery"],
              "network": ("online", "offline"),
              "appearance": simulator.CONTROL_ACTIONS["appearance"]},
}
PERMISSION_ACTIONS = ("grant", "revoke", "reset")
MAX_PUSH_PAYLOAD = 4 * 1024
# A mock body travels inside a JSON request that must fit the 64 KiB body limit.
MAX_MOCK_BODY = 32 * 1024
MAX_MOCK_HEADERS = 64
MAX_TEXT = 1024
MAX_NOTE = 500
MAX_REQUESTS = 200
DEFAULT_REQUESTS = 100
MAX_LINE = 256 * 1024
# Package and bundle ids, and permission or privacy service names. They reach
# `adb shell`, which joins its argv into one shell line, so nothing else passes.
APP_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")
SERVICE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
HTTP_METHOD = re.compile(r"[A-Za-z]{1,16}")
EMULATOR_SERIAL = re.compile(r"emulator-\d+")
# Foreground packages that are the system, not an app worth defaulting to.
NOT_AN_APP = ("launcher", "com.android.systemui", "com.android.settings")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    value.add_argument("--platform", choices=(ANDROID, IOS), required=True)
    value.add_argument("--target", required=True)
    value.add_argument("--tool", required=True)
    value.add_argument("--install-root", action="append", default=[], metavar="DIR",
                       help="a folder installs may come from (repeatable)")
    value.add_argument("--captures-dir", default=None, metavar="DIR",
                       help="the captures root (default: $AUTONOM_CAPTURES_DIR, else "
                            "~/Downloads/Autonom)")
    value.add_argument("--device-name", default=None, metavar="NAME",
                       help="the device's display name, used for its captures folder")
    return value


def _invalid(message: str, hint: str | None = None) -> errors.AutonomError:
    return errors.AutonomError(errors.FLOW_COMMAND_INVALID, message, hint)


def _is_number(value: Any) -> bool:
    # JSON booleans arrive as Python bools, which are ints; never count them.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        return False


def _is_int(value: Any) -> bool:
    return _is_number(value) and value == int(value)


def _optional_text(payload: dict[str, Any], name: str, limit: int = MAX_TEXT) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid(f"{name} must be a string of at most {limit} characters")
    return value


def _required_text(payload: dict[str, Any], name: str, limit: int = MAX_TEXT) -> str:
    value = _optional_text(payload, name, limit)
    if not value:
        raise _invalid(f"{name} is required (a non-empty string)")
    return value


def _optional_bool(payload: dict[str, Any], name: str) -> bool | None:
    value = payload.get(name)
    if value is not None and not isinstance(value, bool):
        raise _invalid(f"{name} must be true or false")
    return value


def _utf8_length(text: str) -> int:
    return len(text.encode("utf-8"))


class Tools:
    """The operations of one Canvas's tools process, bound to its target."""

    def __init__(self, target: Target, *, install_roots: tuple[str, ...] | list[str] = (),
                 captures_dir: str | None = None, device_name: str | None = None) -> None:
        self.target = target
        self.install_roots = [str(root) for root in install_roots if root]
        self.captures_dir = captures_dir
        self.device_name = device_name
        self.recording: dict[str, Any] | None = None
        self.last_recording: dict[str, Any] | None = None
        self.handlers: dict[str, Callable[..., Any]] = {
            "context": self.context,
            "permissions.list": self.permissions_list,
            "location.get": self.location_get,
            "network.status": self.network_status,
            "network.requests": self.network_requests,
            "network.request": self.network_request,
            "mocks.list": self.mocks_list,
            "permissions.set": self.permissions_set,
            "location.set": self.location_set,
            "location.clear": self.location_clear,
            "simulate": self.simulate,
            "network.start": self.network_start,
            "network.attach": self.network_attach,
            "network.detach": self.network_detach,
            "network.stop": self.network_stop,
            "mocks.add": self.mocks_add,
            "mocks.update": self.mocks_update,
            "mocks.enable": self.mocks_enable,
            "mocks.disable": self.mocks_disable,
            "mocks.remove": self.mocks_remove,
            "mocks.clear": self.mocks_clear,
            "captures.list": self.captures_list,
            "record.status": self.record_status,
            "apps.candidates": self.apps_candidates,
            "health": self.health,
            "capture.screenshot": self.capture_screenshot,
            "record.start": self.record_start,
            "record.stop": self.record_stop,
            "app.install": self.app_install,
            "app.launch": self.app_launch,
            "app.open_url": self.app_open_url,
            "app.locale": self.app_locale,
            "captures.delete": self.captures_delete,
            "captures.file": self.captures_file,
        }

    # --- scope -------------------------------------------------------------------

    def session(self) -> dict[str, Any] | None:
        """The live session on this target, else None: the one its target
        pointer names (a session started beside the primary, by a Canvas
        workspace for example), or the current session when it is on this
        target. The bridge's lookup also binds the rest of the operation to
        that session, so a save never moves `current.json` to it.

        An unreadable session record counts as none: the read panels still work.
        """
        try:
            record = bridge.journal_session(self.target)
        except Exception:  # noqa: BLE001 - a corrupt current.json is no session
            return None
        if record is None or record.get("stopped_at"):
            return None
        return record

    def _session_hint(self) -> str:
        flag = (f"--serial {self.target.target_id}" if self.target.platform == ANDROID
                else f"--platform ios --udid {self.target.target_id}")
        return (f"Start one on this target: 'autonom session start {flag} "
                "--app-id <app id>'.")

    def require_session(self, record: dict[str, Any] | None = None) -> dict[str, Any]:
        """`record` (the dispatch's session on this target) or the current one,
        else the `no_active_session` refusal naming how to start one here."""
        record = record if record is not None else self.session()
        if record is None:
            raise errors.AutonomError(
                errors.NO_ACTIVE_SESSION,
                f"no Autonom session on this {self.target.platform} target "
                f"({self.target.target_id})",
                self._session_hint(),
            )
        return record

    @property
    def emulator(self) -> bool:
        return (self.target.platform == IOS
                or bool(EMULATOR_SERIAL.fullmatch(self.target.target_id)))

    def _foreground_app(self) -> str | None:
        if self.target.platform != ANDROID:
            return None
        try:
            component = screenshot.foreground(self.target)
        except Exception:  # noqa: BLE001 - a default, never a failure
            return None
        package = (component or "").split("/", 1)[0]
        if not package or not APP_ID.fullmatch(package):
            return None
        if any(marker in package for marker in NOT_AN_APP):
            return None
        return package

    def _app_id(self, payload: dict[str, Any], record: dict[str, Any] | None,
                *, required: bool) -> str | None:
        """The payload's app id, else this target's session app id."""
        value = payload.get("app_id")
        if value is not None and not isinstance(value, str):
            raise _invalid("app_id must be a string")
        value = (value or "").strip() or (record or {}).get("app_id") or None
        if value is None:
            if required:
                raise errors.AutonomError(
                    errors.INVALID_VALUE,
                    "no app id given and none recorded in a session on this target",
                    "Enter the app's package or bundle id, or start the session "
                    "with --app-id.")
            return None
        if not APP_ID.fullmatch(value):
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"not a package or bundle id: {value!r}",
                "Use letters, digits, '.', '_' or '-', e.g. com.example.app.")
        return value

    # --- read ops ------------------------------------------------------------------

    def context(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.session()
        app_id = (record or {}).get("app_id") or self._foreground_app()
        ios = self.target.platform == IOS
        privacy = list(ios_simctl.privacy_services(self.target.tool)[0]) if ios else None
        aliases = (None if ios else
                   {alias: list(names)
                    for alias, names in device_state.ANDROID_PERMISSION_ALIASES.items()})
        simulate = ({control: list(actions)
                     for control, actions in SIMULATE_SUPPORT[self.target.platform].items()}
                    if self.emulator else {})
        return {
            "platform": self.target.platform,
            "target_id": self.target.target_id,
            "emulator": self.emulator,
            "session": ({"id": record.get("session_id"), "app_id": record.get("app_id")}
                        if record else None),
            "app_id": app_id,
            "privacy_services": privacy,
            "android_permission_aliases": aliases,
            "simulate": simulate,
            "location_readable": not ios,
            "location_clearable": ios,
            "network_available": record is not None,
            "permissions_readable": not ios,
        }

    def permissions_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.session()
        if self.target.platform == IOS:
            app_id = self._app_id(payload, record, required=False)
            services, _source = ios_simctl.privacy_services(self.target.tool)
            return {"app_id": app_id, "readable": False,
                    "permissions": [{"name": name, "alias": None, "granted": None}
                                    for name in services if name != "all"],
                    "note": "simctl cannot read the privacy state back; grant, revoke "
                            "or reset sets it."}
        app_id = self._app_id(payload, record, required=True)
        return {"app_id": app_id, "readable": True,
                "permissions": device_state.app_runtime_permissions(self.target, app_id)}

    def location_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail = device_state.get_location(self.target, self.session())
        return {**detail, **self.target.identity()}

    def network_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.require_session()
        result = cli.network_status_payload(
            record, lambda: self.target, lambda: mocks_mod.session_hit_counts(record))
        result.pop("ok", None)
        return result

    def network_requests(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.require_session()
        maximum = payload.get("max", DEFAULT_REQUESTS)
        if not _is_int(maximum) or not 1 <= maximum <= MAX_REQUESTS:
            raise _invalid(f"max must be an integer from 1 to {MAX_REQUESTS}")
        status = payload.get("status")
        if status is not None and (not _is_int(status) or not 0 <= status <= 999):
            raise _invalid("status must be an HTTP status number")
        method = _optional_text(payload, "method", 16)
        if method and not HTTP_METHOD.fullmatch(method):
            raise _invalid("method must be an HTTP method such as GET")
        return store.listing(
            record, max_items=int(maximum),
            since_id=_optional_text(payload, "since_id", 128) or None,
            host=_optional_text(payload, "host", 255) or None,
            method=method or None,
            status=None if status is None else int(status),
            mocked=_optional_bool(payload, "mocked"),
        )

    def network_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.require_session()
        # The previews only: full bodies are never served to the drawer.
        return {"request": store.find(record, _required_text(payload, "id", 128))}

    def mocks_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.session()
        rules = mocks_mod.load()
        counts = mocks_mod.session_hit_counts(record) if record is not None else {}
        warnings = mocks_mod.annotate_hits(rules, counts)
        result: dict[str, Any] = {"count": len(rules), "mocks": rules,
                                  **mocks_mod.summary()}
        if warnings:
            result["warnings"] = warnings
        return result

    # --- mutating ops: each returns (result, journal words, journal summary) --------

    def permissions_set(self, payload: dict[str, Any], record: dict[str, Any] | None):
        action = payload.get("action")
        if action not in PERMISSION_ACTIONS:
            raise _invalid("action must be one of: " + ", ".join(PERMISSION_ACTIONS))
        service = _required_text(payload, "service", 128)
        if not SERVICE.fullmatch(service):
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"not a permission or privacy service name: {service!r}",
                "Pass a full Android permission (android.permission.CAMERA), a short "
                "name (camera) or an iOS privacy service.")
        app_id = self._app_id(payload, record,
                              required=self.target.platform == ANDROID or action != "reset")
        detail = device_state.permissions(self.target, action, service, app_id)
        return ({**detail, **self.target.identity()},
                [action, service, app_id or "-"], {"app_id": app_id})

    def location_set(self, payload: dict[str, Any], record: dict[str, Any] | None):
        latitude, longitude = payload.get("latitude"), payload.get("longitude")
        if not (_is_number(latitude) and _is_number(longitude)):
            raise errors.AutonomError(
                errors.INVALID_COORDINATES,
                "latitude and longitude must both be numbers",
                "Latitude must be -90..90 and longitude -180..180, e.g. 52.3702, 4.8952.")
        coordinates = f"{float(latitude)!r},{float(longitude)!r}"
        detail = device_state.set_location(self.target, coordinates, record)
        if record is not None:
            session.save(record)
        return ({**detail, **self.target.identity()}, [coordinates],
                {"via": detail.get("via")})

    def location_clear(self, payload: dict[str, Any], record: dict[str, Any] | None):
        device_state.clear_location(self.target)
        return {"location": None, **self.target.identity()}, [], {}

    def _simulate_values(self, control: str, values: Any) -> tuple[dict[str, Any], list[str]]:
        """Checked values, and the journal words that describe them (no payload text)."""
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise _invalid("simulate values must be an object")
        allowed = SIMULATE_VALUES[control]
        unknown = sorted(set(values) - set(allowed))
        if unknown:
            raise _invalid(f"simulate {control} does not take: {', '.join(unknown)}",
                           f"Values it takes: {', '.join(allowed) or 'none'}.")
        words: list[str] = []
        for key, value in values.items():
            if control == "push" and key == "payload":
                if isinstance(value, str):
                    size = _utf8_length(value)
                elif isinstance(value, dict):
                    size = _utf8_length(json.dumps(value, ensure_ascii=False,
                                                   separators=(",", ":")))
                else:
                    raise _invalid("push payload must be a JSON object")
                if size > MAX_PUSH_PAYLOAD:
                    raise _invalid(f"push payload is {size} bytes; at most "
                                   f"{MAX_PUSH_PAYLOAD} are allowed")
                words.append(f"payload_len={size}")
                continue
            if isinstance(value, str):
                if len(value) > 256:
                    raise _invalid(f"simulate value {key} is too long")
            elif not (_is_number(value) or isinstance(value, bool)):
                raise _invalid(f"simulate value {key} must be a string or a number")
            if key == "app_id" and not APP_ID.fullmatch(str(value)):
                raise errors.AutonomError(errors.INVALID_VALUE,
                                          f"not a bundle id: {value!r}")
            words.append(f"{key}={value}")
        return values, words

    def simulate(self, payload: dict[str, Any], record: dict[str, Any] | None):
        control = payload.get("control")
        if control not in SIMULATE_CONTROLS:
            raise _invalid(f"simulate control {control!r} is not allowed here",
                           "Allowed controls: " + ", ".join(SIMULATE_CONTROLS) + ".")
        action = payload.get("action")
        if not isinstance(action, str) or not action or len(action) > 32:
            raise _invalid("simulate action must be a non-empty string")
        values, words = self._simulate_values(control, payload.get("values"))
        # `session` is where push takes its default app id: only this target's.
        result = simulator.apply(self.target, control, action, dict(values),
                                 session=record or {})
        if control == "push" and result.get("app_id"):
            words = [word for word in words if not word.startswith("app_id=")]
            words.append(f"app_id={result['app_id']}")
        summary = {"app_id": result["app_id"]} if result.get("app_id") else {}
        return {**result, **self.target.identity()}, [control, action, *words], summary

    def network_start(self, payload: dict[str, Any], record: dict[str, Any] | None):
        record = self.require_session(record)
        # Never --capture-bodies, and consent only from an explicit `true`.
        result = cli.network_start_payload(
            record, port=None, capture_bodies=False,
            acknowledged=payload.get("acknowledged") is True)
        result.pop("ok", None)
        return result, ["--i-understand-mitm"], {"port": result.get("port")}

    def network_attach(self, payload: dict[str, Any], record: dict[str, Any] | None):
        record = self.require_session(record)
        # The default mode: no --system-ca and no --install-ca.
        result = cli.network_attach_payload(
            record, lambda: self.target,
            acknowledged=payload.get("acknowledged") is True)
        result.pop("ok", None)
        return result, ["--i-understand-mitm"], {}

    def network_detach(self, payload: dict[str, Any], record: dict[str, Any] | None):
        record = self.require_session(record)
        result = cli.network_detach_payload(record, self.target)
        result.pop("ok", None)
        return result, [], {}

    def network_stop(self, payload: dict[str, Any], record: dict[str, Any] | None):
        record = self.require_session(record)
        result = cli.network_stop_payload(record, lambda: self.target)
        result.pop("ok", None)
        return result, [], {}

    def _mock_fields(self, payload: dict[str, Any], *, adding: bool) -> tuple[dict[str, Any], list[str]]:
        """The mock fields the payload sets, checked; and their journal words."""
        fields: dict[str, Any] = {}
        words: list[str] = []
        if adding or "url_glob" in payload:
            url_glob = payload.get("url_glob")
            if url_glob is not None and not isinstance(url_glob, str):
                raise _invalid("url_glob must be a string")
            if url_glob is not None and len(url_glob) > 2048:
                raise _invalid("url_glob must be at most 2048 characters")
            mocks_mod.require_target((url_glob or "").strip() or None)
            fields["url_glob"] = url_glob.strip()
            # A glob is a URL more often than not: its query values are masked.
            words.append((redact.scrub_url(fields["url_glob"]) or fields["url_glob"])
                         if "://" in fields["url_glob"] else fields["url_glob"])
        if "method" in payload:
            method = _optional_text(payload, "method", 16)
            if method and not HTTP_METHOD.fullmatch(method):
                raise _invalid("method must be an HTTP method such as GET")
            fields["method"] = method or ("" if not adding else None)
            if method:
                words.append(f"method={method.upper()}")
        if "host" in payload:
            host = _optional_text(payload, "host", 255)
            fields["host"] = host or ("" if not adding else None)
        if "ignore_query" in payload:
            fields["ignore_query"] = _optional_bool(payload, "ignore_query")
        if adding or "status" in payload:
            status = payload.get("status", 200 if adding else None)
            if status is not None and not _is_int(status):
                raise _invalid("status must be an integer HTTP status")
            if status is not None:
                mocks_mod.validate_response(status=int(status))
                fields["status"] = int(status)
                words.append(f"status={int(status)}")
        if "headers" in payload and payload["headers"] is not None:
            headers = payload["headers"]
            if (not isinstance(headers, dict) or len(headers) > MAX_MOCK_HEADERS
                    or not all(isinstance(name, str) and name.strip() and len(name) <= 256
                               and isinstance(value, str) and len(value) <= 8192
                               for name, value in headers.items())):
                raise _invalid("headers must be an object of name: value strings",
                               f"At most {MAX_MOCK_HEADERS} headers, each a non-empty name "
                               "with a string value.")
            fields["headers"] = {name.strip(): value for name, value in headers.items()}
            words.append(f"headers={len(headers)}")
        if "body" in payload and payload["body"] is not None:
            body = payload["body"]
            if not isinstance(body, str):
                raise _invalid("body must be a string")
            if _utf8_length(body) > MAX_MOCK_BODY:
                raise errors.AutonomError(
                    errors.INVALID_VALUE,
                    f"the body is {_utf8_length(body)} bytes; at most {MAX_MOCK_BODY} "
                    "are allowed here",
                    "Add a larger body with 'autonom network mock add --body-file'.")
            mocks_mod.validate_response(body_text=body)
            fields["body_text"] = body
            words.append(f"body_len={_utf8_length(body)}")
        if "note" in payload:
            note = _optional_text(payload, "note", MAX_NOTE)
            fields["note"] = note or ("" if not adding else None)
        return fields, words

    def _mock_id(self, payload: dict[str, Any]) -> str:
        return _required_text(payload, "id", 64)

    @staticmethod
    def _mock_result(rule: dict[str, Any], **extra: Any) -> dict[str, Any]:
        # The mock itself, and under `mock` as the CLI reports it.
        return {**rule, "mock": rule, **extra}

    def mocks_add(self, payload: dict[str, Any], record: dict[str, Any] | None):
        fields, words = self._mock_fields(payload, adding=True)
        headers = fields.get("headers")
        body = fields.get("body_text")
        if not headers and body is not None and body.lstrip()[:1] in ("{", "["):
            headers = {"Content-Type": "application/json"}  # as `mock add --json`
        rule = mocks_mod.add(
            url_glob=fields["url_glob"], method=fields.get("method"),
            host=fields.get("host"), ignore_query=bool(fields.get("ignore_query")),
            status=fields["status"], headers=headers or {}, body_text=body,
            note=fields.get("note"),
        )
        return (self._mock_result(rule, registry=str(mocks_mod.registry_file())),
                [rule["id"], *words], {})

    def mocks_update(self, payload: dict[str, Any], record: dict[str, Any] | None):
        identifier = self._mock_id(payload)
        fields, words = self._mock_fields(payload, adding=False)
        rule = mocks_mod.update(
            identifier, url_glob=fields.get("url_glob"), method=fields.get("method"),
            host=fields.get("host"), ignore_query=fields.get("ignore_query"),
            status=fields.get("status"), headers=fields.get("headers"),
            body_text=fields.get("body_text"), note=fields.get("note"),
        )
        return self._mock_result(rule), [identifier, *words], {}

    def _mocks_toggle(self, payload: dict[str, Any], enabled: bool):
        identifier = self._mock_id(payload)
        detail = mocks_mod.set_enabled(identifier, enabled)
        return (self._mock_result(detail["mock"], changed=detail["changed"],
                                  enabled=detail["enabled"]), [identifier], {})

    def mocks_enable(self, payload: dict[str, Any], record: dict[str, Any] | None):
        return self._mocks_toggle(payload, True)

    def mocks_disable(self, payload: dict[str, Any], record: dict[str, Any] | None):
        return self._mocks_toggle(payload, False)

    def mocks_remove(self, payload: dict[str, Any], record: dict[str, Any] | None):
        identifier = self._mock_id(payload)
        return mocks_mod.remove(identifier), [identifier], {}

    def mocks_clear(self, payload: dict[str, Any], record: dict[str, Any] | None):
        detail = mocks_mod.clear()
        return ({"removed": detail["cleared"], **detail},
                [f"removed={detail['cleared']}"], {"count": detail["cleared"]})

    # --- actions: captures ---------------------------------------------------------

    def _captures_root(self) -> Path:
        return captures.root(self.captures_dir)

    def _captures_path(self) -> Path:
        """This device's captures folder (not created)."""
        return captures.device_path(self._captures_root(), self.device_name,
                                    self.target.target_id)

    def _captures_dir(self) -> Path:
        """This device's captures folder, created 0700 when missing."""
        return captures.device_dir(self._captures_root(), self.device_name,
                                   self.target.target_id)

    def _meta(self, **extra: Any) -> dict[str, Any]:
        return {"platform": self.target.platform, "target_id": self.target.target_id,
                "device_name": self.device_name, **extra}

    def _capture_name(self, payload: dict[str, Any]) -> str:
        name = payload.get("name")
        if not isinstance(name, str) or not name or len(name) > 255:
            raise _invalid("name must be a capture file name")
        return name

    def captures_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = payload.get("limit", captures.LIST_DEFAULT)
        if not _is_int(limit) or not 1 <= limit <= captures.LIST_MAX:
            raise _invalid(f"limit must be an integer from 1 to {captures.LIST_MAX}")
        return captures.list_entries(self._captures_path(), self.target.target_id, int(limit))

    def captures_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = self._capture_name(payload)
        captures.delete(self._captures_path(), name)
        return {"removed": name}

    def captures_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        return captures.entry(self._captures_path(), self._capture_name(payload))

    def capture_screenshot(self, payload: dict[str, Any], record: dict[str, Any] | None):
        directory = self._captures_dir()
        temp = captures.temp_path(directory, "png")
        created = time.time()
        try:
            screenshot.capture_target(self.target, temp)
        except BaseException:
            _unlink(temp)
            raise
        capture, pruned = captures.commit(temp, directory, "screenshot",
                                          meta=self._meta(created=created, duration_ms=None))
        return ({"capture": capture, "pruned": pruned}, [capture["name"]],
                {"size": capture["size"], "pruned": len(pruned) or None})

    # --- actions: recording ----------------------------------------------------------

    def _recording_active(self) -> bool:
        return self.recording is not None and not self.recording.get("finalized")

    def record_start(self, payload: dict[str, Any], record: dict[str, Any] | None):
        self._settle_recording()
        if self._recording_active():
            raise errors.AutonomError(
                errors.RECORDING_ALREADY_ACTIVE, "a recording is already running on this device",
                "Stop it first; one recording runs per device.")
        directory = self._captures_dir()
        temp = captures.temp_path(directory, "mp4")
        if self.target.platform == ANDROID:
            remote = f"/sdcard/autonom-canvas-{secrets.token_hex(6)}.mp4"
            argv = [self.target.tool, "-s", self.target.target_id, "shell", "screenrecord",
                    "--time-limit", str(RECORD_LIMIT_S), remote]
        else:
            remote = None
            argv = [self.target.tool, "simctl", "io", self.target.target_id, "recordVideo",
                    "--codec", "h264", "--force", str(temp)]
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is constructed, never a shell
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            _unlink(temp)
            raise errors.AutonomError(errors.BACKEND_FAILED,
                                      f"the recorder could not start: {exc}") from None
        started = time.time()
        state: dict[str, Any] = {
            "process": process, "remote": remote, "temp": temp, "directory": directory,
            "started": started, "started_mono": time.monotonic(),
            "started_at": captures._now_iso(started),  # noqa: SLF001 - one ISO format
            "limit_hit": False, "finalized": False, "timer": None,
        }
        if self.target.platform == IOS:
            # simctl records until told to stop: a timer stops it at the limit.
            timer = threading.Timer(RECORD_LIMIT_S, self._limit_reached, args=(state,))
            timer.daemon = True
            timer.start()
            state["timer"] = timer
        self.recording = state
        return ({"recording": {"started_at": state["started_at"], "limit_s": RECORD_LIMIT_S}},
                [remote or "simctl"], {})

    def _limit_reached(self, state: dict[str, Any]) -> None:
        """The iOS limit timer: SIGINT, so simctl finishes the file."""
        process = state["process"]
        if process.poll() is None and not state.get("stopping"):
            state["limit_hit"] = True
            _signal(process, signal.SIGINT)

    def _stop_recorder(self, state: dict[str, Any]) -> None:
        """Ask the recorder to finish its file and wait for it (at most 10 s)."""
        process = state["process"]
        state["stopping"] = True
        if state.get("timer") is not None:
            state["timer"].cancel()
        if process.poll() is not None:
            return
        if self.target.platform == ANDROID:
            try:
                completed = adb_mod.run_adb(
                    self.target.tool, ["shell", "pkill", "-INT", "-f", state["remote"]],
                    serial=self.target.target_id, timeout=RECORD_STOP_WAIT_S, check=False)
                stopped = completed.returncode == 0
            except Exception:  # noqa: BLE001 - a hung or missing adb falls back to SIGINT
                stopped = False
            if not stopped:
                _signal(process, signal.SIGINT)
        else:
            _signal(process, signal.SIGINT)
        try:
            process.wait(timeout=RECORD_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            _signal(process, signal.SIGINT)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _remove_remote(self, state: dict[str, Any], timeout: float) -> None:
        if state.get("remote"):
            try:
                adb_mod.run_adb(self.target.tool, ["shell", "rm", "-f", state["remote"]],
                                serial=self.target.target_id, timeout=timeout, check=False)
            except Exception:  # noqa: BLE001 - cleanup never raises
                pass

    def _finalize(self, state: dict[str, Any], ended_early: bool) -> dict[str, Any]:
        """Bring the finished video into the captures folder; the device file is
        always removed. Returns the record.stop result."""
        state["finalized"] = True
        duration_ms = int((time.monotonic() - state["started_mono"]) * 1000)
        temp = state["temp"]
        try:
            if self.target.platform == ANDROID:
                time.sleep(RECORD_SETTLE_S)  # screenrecord writes its index after the signal
                adb_mod.run_adb(self.target.tool, ["pull", state["remote"], str(temp)],
                                serial=self.target.target_id, timeout=RECORD_PULL_TIMEOUT_S,
                                check=True)
        except BaseException:
            _unlink(temp)
            raise
        finally:
            self._remove_remote(state, 30)
        capture, pruned = captures.commit(
            temp, state["directory"], "video",
            meta=self._meta(created=state["started"], duration_ms=duration_ms))
        result = {"capture": capture, "pruned": pruned, "duration_ms": duration_ms,
                  "ended_early": ended_early}
        self.last_recording = {"capture": capture, "ended_early": ended_early}
        return result

    def _settle_recording(self) -> dict[str, Any] | None:
        """A recording that ended on its own (the 180 s limit) is saved now."""
        state = self.recording
        if state is None or state.get("finalized") or state["process"].poll() is None:
            return None
        self.recording = None
        if state.get("timer") is not None:
            state["timer"].cancel()
        try:
            return self._finalize(state, ended_early=True)
        except errors.AutonomError as exc:
            self.last_recording = {"capture": None, "ended_early": True,
                                   "error": exc.message, "error_code": exc.code}
            return None

    def record_stop(self, payload: dict[str, Any], record: dict[str, Any] | None):
        state = self.recording
        if state is None or state.get("finalized"):
            raise errors.AutonomError(
                errors.RECORDING_NOT_ACTIVE, "no recording is running on this device",
                "Start one with Record first.")
        ended_early = state["process"].poll() is not None or state.get("limit_hit", False)
        try:
            self._stop_recorder(state)
        except BaseException:
            # The recorder could not be stopped cleanly: drop the recording, but never
            # leave the local recorder running or its device file behind.
            self._abandon(state, CLEANUP_TIMEOUT_S)
            self.recording = None
            raise
        self.recording = None
        ended_early = ended_early or state.get("limit_hit", False)
        result = self._finalize(state, ended_early=ended_early)
        return (result, [result["capture"]["name"]],
                {"duration_ms": result["duration_ms"],
                 "pruned": len(result["pruned"]) or None})

    def record_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._settle_recording()
        state = self.recording if self._recording_active() else None
        return {
            "recording": state is not None,
            "started_at": state["started_at"] if state else None,
            "elapsed_ms": (int((time.monotonic() - state["started_mono"]) * 1000)
                           if state else None),
            "limit_s": RECORD_LIMIT_S,
            "last": self.last_recording,
        }

    def _abandon(self, state: dict[str, Any], budget: float) -> None:
        """Drop a recording without saving it: kill the local recorder, stop the device
        recorder and remove its file, and delete the partial file, all within `budget`
        seconds. Never raises."""
        deadline = time.monotonic() + max(budget, 1.0)
        state["finalized"] = True
        state["stopping"] = True
        if state.get("timer") is not None:
            state["timer"].cancel()
        process = state["process"]
        if process.poll() is None:
            _signal(process, signal.SIGINT)
            try:
                process.wait(timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        if self.target.platform == ANDROID and state.get("remote"):
            # The device file first (the part that must not be left behind), then a
            # screenrecord still running there; both within what is left of the budget.
            self._remove_remote(state, max(1.0, deadline - time.monotonic() - 0.5))
            left = deadline - time.monotonic()
            if left >= 0.2:
                try:
                    adb_mod.run_adb(self.target.tool,
                                    ["shell", "pkill", "-INT", "-f", state["remote"]],
                                    serial=self.target.target_id, timeout=left, check=False)
                except Exception:  # noqa: BLE001 - cleanup never raises
                    pass
        _unlink(state["temp"])

    def shutdown(self) -> None:
        """At exit: stop a running recorder, remove its device file and partial file,
        within SHUTDOWN_BUDGET_S (inside the Node side's close grace). Nothing is saved."""
        state, self.recording = self.recording, None
        if state is None or state.get("finalized"):
            return
        self._abandon(state, SHUTDOWN_BUDGET_S)

    # --- actions: apps ---------------------------------------------------------------

    def _action_app_id(self, payload: dict[str, Any]) -> str:
        value = payload.get("app_id")
        if not isinstance(value, str) or len(value) > 255 or not ACTION_APP_ID.fullmatch(value):
            raise errors.AutonomError(
                errors.INVALID_VALUE, "app_id must be a package or bundle id such as com.example.app",
                "Use letters, digits and '_' in dot-separated parts ('-' after the first).")
        return value

    def _not_allowed(self, why: str) -> errors.AutonomError:
        return errors.AutonomError(
            errors.INSTALL_PATH_NOT_ALLOWED, f"the install path is not allowed: {why}",
            "Pick a build inside one of the Canvas's --install-root folders.",
            roots=list(self.install_roots))

    def _install_path(self, payload: dict[str, Any]) -> str:
        """The realpath of an allowed install path (rules of contract 5.1)."""
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw:
            raise _invalid("path is required (a non-empty string)")
        if len(raw) > MAX_INSTALL_PATH:
            raise self._not_allowed(f"longer than {MAX_INSTALL_PATH} characters")
        if CONTROL_CHARS.search(raw):
            raise self._not_allowed("it contains control characters")
        if not os.path.isabs(raw):
            raise self._not_allowed("it is not an absolute path")
        real = os.path.realpath(raw)
        inside = False
        for root in self.install_roots:
            base = os.path.realpath(os.path.expanduser(root))
            if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
                inside = True
                break
        if not inside:
            raise self._not_allowed("it is outside every --install-root folder")
        if not os.path.exists(real):
            raise errors.AutonomError(errors.INSTALL_PATH_NOT_FOUND,
                                      f"nothing to install at {raw}",
                                      "Build the app first, or pick one of the listed builds.")
        if self.target.platform == ANDROID:
            if not (os.path.isfile(real) and real.lower().endswith(".apk")):
                raise errors.AutonomError(errors.INVALID_VALUE,
                                          "an Android install takes an .apk file",
                                          "Pick a built .apk (for example build/app/outputs/...).")
        elif not (os.path.isdir(real) and real.endswith(".app")
                  and os.path.isfile(os.path.join(real, "Info.plist"))):
            raise errors.AutonomError(errors.INVALID_VALUE,
                                      "an iOS Simulator install takes an .app folder with Info.plist",
                                      "Pick a Simulator build (.app) from DerivedData or build/ios.")
        return real

    def app_install(self, payload: dict[str, Any], record: dict[str, Any] | None):
        if not self.install_roots:
            raise errors.AutonomError(errors.INSTALL_NOT_CONFIGURED,
                                      "installs are off: this Canvas has no install folder",
                                      INSTALL_HINT)
        allow_downgrade = _optional_bool(payload, "allow_downgrade") is True
        path = self._install_path(payload)
        started = time.monotonic()
        if self.target.platform == ANDROID:
            size = os.path.getsize(path)
            argv = ["install", "-r", *(["-d"] if allow_downgrade else []), path]
            completed = adb_mod.run_adb(self.target.tool, argv, serial=self.target.target_id,
                                        timeout=INSTALL_TIMEOUT_S, check=False)
            output = completed.stdout if isinstance(completed.stdout, str) else ""
            if completed.returncode != 0 or "Failure" in output:
                match = INSTALL_REASON.search(output)
                raise errors.AutonomError(
                    errors.INSTALL_FAILED,
                    f"the device refused the install: {_last_line(output) or completed.returncode}",
                    "An older build signed with another key or a higher version needs an "
                    "uninstall (or allow downgrade) first.",
                    reason=match.group(1) if match else None)
            app_id = None
        else:
            size = _tree_size(path)
            try:
                ios_simctl.install(self.target.tool, self.target.target_id, Path(path))
            except errors.AutonomError as exc:
                if exc.code != errors.BACKEND_FAILED:
                    raise
                raise errors.AutonomError(errors.INSTALL_FAILED,
                                          f"the Simulator refused the install: {exc.message}",
                                          "Check the build is for the Simulator, not a device.",
                                          reason=None) from None
            app_id = ios_simctl.bundle_identifier(path)
        duration_ms = int((time.monotonic() - started) * 1000)
        return ({"installed": path, "app_id": app_id, "size": size, "duration_ms": duration_ms},
                [path], {"app_id": app_id})

    def apps_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.install_roots:
            return {"configured": False, "roots": [], "candidates": [], "truncated": False,
                    "hint": INSTALL_HINT}
        android = self.target.platform == ANDROID
        found: list[dict[str, Any]] = []
        deadline = time.monotonic() + CANDIDATE_MAX_SECONDS
        visited = 0
        truncated = False
        seen: set[str] = set()
        for root in self.install_roots:
            base = os.path.realpath(os.path.expanduser(root))
            stack = [(base, 0)]
            while stack and not truncated:
                folder, depth = stack.pop()
                try:
                    entries = list(os.scandir(folder))
                except OSError:
                    continue
                for item in entries:
                    visited += 1
                    if visited > CANDIDATE_MAX_ENTRIES or time.monotonic() > deadline:
                        truncated = True
                        break
                    if item.is_symlink():
                        continue
                    name = item.name
                    if item.is_dir(follow_symlinks=False):
                        if name.startswith(".") or name in CANDIDATE_SKIP:
                            continue
                        if (not android and name.endswith(".app")
                                and os.path.isfile(os.path.join(item.path, "Info.plist"))):
                            if item.path not in seen:
                                seen.add(item.path)
                                found.append(_candidate(item.path, "app", _tree_size(item.path)))
                            continue
                        if depth + 1 <= CANDIDATE_DEPTH:
                            stack.append((item.path, depth + 1))
                    elif android and name.lower().endswith(".apk") and item.is_file(follow_symlinks=False):
                        if item.path not in seen:
                            seen.add(item.path)
                            found.append(_candidate(item.path, "apk",
                                                    item.stat(follow_symlinks=False).st_size))
            if truncated:
                break
        found.sort(key=lambda entry: entry["modified_at"], reverse=True)
        return {"configured": True, "roots": list(self.install_roots),
                "candidates": found[:CANDIDATE_COUNT], "truncated": truncated}

    def app_launch(self, payload: dict[str, Any], record: dict[str, Any] | None):
        app_id = self._action_app_id(payload)
        fresh = _optional_bool(payload, "fresh") is True
        if self.target.platform == IOS:
            env = device_proxy_ios.launch_environment(record) if record else {}
            if fresh:
                ios_simctl.terminate(self.target.tool, self.target.target_id, app_id)
            pid = ios_simctl.launch(self.target.tool, self.target.target_id, app_id, env=env)
            result = {"launched": app_id, "pid": pid, "mode": "fresh" if fresh else "resume"}
        else:
            launch = session.launch_app_fresh if fresh else session.launch_app
            detail = launch(self.target.tool, self.target.target_id, app_id)
            result = {"launched": app_id, **detail}
        return ({**result, **self.target.identity()},
                [app_id, *(["--fresh"] if fresh else [])], {"app_id": app_id})

    def app_open_url(self, payload: dict[str, Any], record: dict[str, Any] | None):
        url = payload.get("url")
        if (not isinstance(url, str) or not url or len(url) > 2048
                or not URL_SCHEME.match(url) or any(ch.isspace() for ch in url)
                or CONTROL_CHARS.search(url)):
            raise errors.AutonomError(
                errors.INVALID_VALUE, "url must be a URL with a scheme, at most 2048 characters, "
                "without spaces", "For example myapp://profile/42 or https://example.com/x.")
        if self.target.platform == ANDROID:
            detail = cli._open_android(self.target, url)  # noqa: SLF001 - the CLI's `open`
        else:
            device_state.open_url(self.target, url)
            detail = {"handled_by": "unknown"}
        result = {"opened": True, **detail,
                  "handled_by": detail.get("handled_by") or "unknown", **self.target.identity()}
        origin = url_origin(url)
        return result, [origin], {"url": origin}

    def app_locale(self, payload: dict[str, Any], record: dict[str, Any] | None):
        app_id = self._action_app_id(payload)
        locale = payload.get("locale")
        match = LOCALE.fullmatch(locale) if isinstance(locale, str) and len(locale) <= 20 else None
        if match is None:
            raise errors.AutonomError(errors.INVALID_VALUE,
                                      "locale must be a language tag such as de-DE or pt-BR",
                                      "Language (2-3 letters), optional script, optional region.")
        if self.target.platform == ANDROID:
            sdk = _sdk_level(self.target)
            if sdk is None or sdk < APP_LOCALES_MIN_SDK:
                raise errors.AutonomError(
                    errors.UNSUPPORTED_CAPABILITY,
                    f"per-app language needs Android 13 (API {APP_LOCALES_MIN_SDK}); this device "
                    f"reports API {sdk if sdk is not None else 'unknown'}",
                    "Change the system language in Settings instead.",
                    capability="app.locale")
            completed = adb_mod.run_adb(
                self.target.tool,
                ["shell", "cmd", "locale", "set-app-locales", app_id, "--locales", locale],
                serial=self.target.target_id, timeout=30, check=False)
            output = completed.stdout if isinstance(completed.stdout, str) else ""
            if completed.returncode != 0 or "Exception" in output or output.startswith("Error"):
                raise errors.AutonomError(
                    errors.BACKEND_FAILED,
                    f"the language was not set: {_last_line(output) or completed.returncode}",
                    "Check the app is installed.")
            restarted = False
        else:
            lang, region = match.group("lang"), match.group("region")
            apple_locale = f"{lang.replace('-', '_')}_{region}" if region else lang.replace("-", "_")
            env = device_proxy_ios.launch_environment(record) if record else {}
            ios_simctl.terminate(self.target.tool, self.target.target_id, app_id)
            ios_simctl.launch(self.target.tool, self.target.target_id, app_id,
                              args=["-AppleLanguages", f"({lang})", "-AppleLocale", apple_locale],
                              env=env)
            restarted = True
        return ({"app_id": app_id, "locale": locale, "restarted": restarted},
                [app_id, locale], {"app_id": app_id})

    def health(self, payload: dict[str, Any]) -> dict[str, Any]:
        return tool_health.check(refresh=_optional_bool(payload, "refresh") is True)

    # --- dispatch ------------------------------------------------------------------

    def _journal(self, record: dict[str, Any] | None, op: str, origin: str,
                 words: list[str], summary: dict[str, Any], ok: bool,
                 error_code: str | None = None) -> None:
        if record is None:
            return
        payload = {key: value for key, value in summary.items() if value is not None}
        journal.record_action(
            record, verb=f"canvas {op}", argv=["canvas", op, *map(str, words)],
            payload=payload or None, ok=ok, error_code=error_code, origin=origin,
        )

    def dispatch(self, message: Any) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise _invalid("a tools request must be a JSON object")
        origin = message.get("origin")
        if origin not in ORIGINS:
            raise _invalid("Canvas action origin is invalid")
        op = message.get("op")
        if op not in OPS:
            raise _invalid(f"unknown tools op {op!r}; valid ops: " + ", ".join(OPS),
                           "Send one of the ops the Canvas tools contract lists.")
        payload = message.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise _invalid(f"{op} payload must be an object")
        handler = self.handlers[op]
        if op in UNJOURNALED_OPS:
            return handler(payload)
        record = self.session()
        try:
            result, words, summary = handler(payload, record)
        except errors.AutonomError as exc:
            self._journal(record, op, origin, [], {}, False, exc.code)
            raise
        except Exception:
            self._journal(record, op, origin, [], {}, False, errors.BACKEND_FAILED)
            raise
        self._journal(record, op, origin, words, summary, True)
        return result


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _signal(process: subprocess.Popen, signum: int) -> None:
    try:
        process.send_signal(signum)
    except (ProcessLookupError, OSError):
        pass


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def _tree_size(path: str) -> int:
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _candidate(path: str, kind: str, size: int) -> dict[str, Any]:
    try:
        modified = os.lstat(path).st_mtime
    except OSError:
        modified = 0.0
    return {"path": path, "kind": kind, "size": size,
            "modified_at": captures._now_iso(modified)}  # noqa: SLF001 - one ISO format


def _sdk_level(target: Target) -> int | None:
    completed = adb_mod.run_adb(target.tool, ["shell", "getprop", "ro.build.version.sdk"],
                                serial=target.target_id, timeout=20, check=False)
    text = (completed.stdout if isinstance(completed.stdout, str) else "").strip()
    return int(text) if text.isdigit() else None


def url_origin(url: str) -> str:
    """Scheme and host only: what a journal or log may say about a URL."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return url.split(":", 1)[0] + ":"
    scheme = parts.scheme or url.split(":", 1)[0]
    return f"{scheme}://{host}" if host else f"{scheme}:"


def _request_id(message: Any) -> Any:
    if isinstance(message, dict):
        value = message.get("id")
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            return value
    return None


def failure(request_id: Any, exc: errors.AutonomError) -> dict[str, Any]:
    body = exc.as_dict()
    body.setdefault("hint", None)
    body.setdefault("capability", None)
    return {**body, "id": request_id, "ok": False}


def respond(tools: Tools, line: str) -> dict[str, Any]:
    """The reply to one request line. Never raises."""
    message: Any = None
    try:
        if len(line) > MAX_LINE:
            raise _invalid(f"a tools request may be at most {MAX_LINE} bytes")
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise _invalid(f"a tools request must be one JSON object per line: {exc}") \
                from None
        result = tools.dispatch(message)
        return {"id": _request_id(message), "ok": True, "result": result}
    except errors.AutonomError as exc:
        return failure(_request_id(message), exc)
    except Exception as exc:  # noqa: BLE001 - one request must not stop the process
        return failure(_request_id(message), errors.AutonomError(
            errors.BACKEND_FAILED, str(exc) or exc.__class__.__name__))


def encode(response: dict[str, Any]) -> str:
    try:
        return json.dumps(response, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        return json.dumps(failure(response.get("id"), errors.AutonomError(
            errors.BACKEND_FAILED, f"the result could not be encoded: {exc}")))


def _protocol_stream() -> Any:
    """Keep the real stdout for replies; point descriptor 1 at stderr.

    Library code that prints, and every child process it starts, then writes
    to stderr and can never put a stray line between two replies.
    """
    sys.stdout.flush()
    descriptor = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)


def main() -> int:
    args = parser().parse_args()
    target = Target(args.platform, args.target, args.tool,
                    {"serial": args.target} if args.platform == ANDROID
                    else {"udid": args.target})
    tools = Tools(target, install_roots=args.install_root, captures_dir=args.captures_dir,
                  device_name=args.device_name)
    # Partial files a stopped process left in this device's folder (never created here).
    captures.sweep(tools._captures_path())  # noqa: SLF001
    out = _protocol_stream()
    signal.signal(signal.SIGTERM, _terminate)
    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            out.write(encode(respond(tools, line)) + "\n")
            out.flush()
    finally:
        # stdin closed or SIGTERM: a running recording is dropped, its files removed.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        tools.shutdown()
    return 0


def _terminate(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    raise SystemExit(main())
