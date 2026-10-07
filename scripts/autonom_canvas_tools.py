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
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autonom as cli  # noqa: E402  (the shared network helpers)
import autonom_canvas_bridge as bridge  # noqa: E402
from autonom_lib import device_state, errors, ios_simctl, journal  # noqa: E402
from autonom_lib import screenshot, session, simulator  # noqa: E402
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
OPS = READ_OPS + MUTATING_OPS

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

    def __init__(self, target: Target) -> None:
        self.target = target
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
        }

    # --- scope -------------------------------------------------------------------

    def session(self) -> dict[str, Any] | None:
        """The current session when it is on this target and running, else None.

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
        result = cli.network_stop_payload(record)
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
        if op in READ_OPS:
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
    tools = Tools(target)
    out = _protocol_stream()
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        out.write(encode(respond(tools, line)) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
