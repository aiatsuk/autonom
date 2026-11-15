"""The push message (`autonom.push/v1`): one notification described once and
rendered for each platform.

Pure functions, no device calls. `simulator push compose` shows what a message
becomes on iOS (the APNs payload `simctl push` takes) and on Android (the
string extras of an FCM message, the way Google Play services hands it to an
app), and `simulator push send` delivers exactly that render.

A message is a JSON object with the fields in `FIELDS`; `schemas/push-v1.
schema.json` describes the same object and a test keeps the two in step. On
the command line the fields come from `--json` and `--value` (`from_values`),
optionally laid over a saved file (`file=<path>`, convention
`.autonom/pushes/<name>.json`).

Errors never quote a field's text: a notification title or body can hold
anything a tester typed, and errors are journaled and logged.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import errors

SCHEMA = "autonom.push/v1"
# Every field a message may hold, in the order a saved file lists them.
FIELDS = ("schema", "name", "description", "title", "subtitle", "body", "badge", "sound",
          "category", "thread", "channel", "image", "silent", "data", "message_id", "fcm",
          "ios", "android")
# Non-empty text that reaches the device.
TEXT_FIELDS = ("title", "subtitle", "body", "sound", "category", "thread", "channel", "image",
               "message_id")
# Free text carried in files only (never rendered).
LABEL_FIELDS = ("name", "description")
BOOL_FIELDS = ("silent", "fcm")
ALERT_FIELDS = ("title", "subtitle", "body")
# `data` keys refused on both platforms, so one message is valid everywhere:
# APNs reserves `aps`; FCM reserves these names and the two prefixes.
RESERVED_DATA_KEYS = ("aps", "from", "message_type", "collapse_key")
RESERVED_DATA_PREFIXES = ("google.", "gcm.")
# The values the push actions take besides the message fields.
ACTION_KEYS = ("app_id", "payload", "file", "delivery", "receiver", "action",
               "include_stopped", "write", "overwrite", "platform")
COMPOSE_KEYS = ("file", "write", "overwrite", "platform")
SEND_KEYS = ("app_id", "payload", "file", "delivery", "receiver", "action", "include_stopped")
PLATFORMS = ("ios", "android")
DELIVERIES = ("app", "shade")
IOS_PAYLOAD_LIMIT = 4096  # bytes; APNs refuses a larger payload
FCM_SENDER = "autonom"
FCM_TTL_SECONDS = "3600"
FILE_CONVENTION = ".autonom/pushes/<name>.json"

MESSAGE_HINT = (
    "Describe the push with message fields, e.g. --value title=Hello --value body='From Autonom' "
    "--value data.order_id=42 (or --json '{\"title\": \"Hello\", \"data\": {\"order_id\": 42}}'), "
    "or load a saved one with --value file=" + FILE_CONVENTION + ". "
    "'autonom simulator push compose' shows what each platform gets.")


def _field_hint(field: str) -> str:
    """How to give one field right (the hint of a refusal that names it)."""
    name = field.split(".", 1)[0]
    if name in TEXT_FIELDS:
        return f"Pass {name} as non-empty text, e.g. --value {name}=..."
    if name in LABEL_FIELDS:
        return f"Pass {name} as text."
    if name in BOOL_FIELDS:
        return f"Pass {name}=true or {name}=false."
    if name == "data":
        return ("Pass data as a JSON object, e.g. --json '{\"data\": {\"order_id\": 42}}', "
                "or one key with --value data.<key>=<text>.")
    if name == "ios":
        return ("Pass ios as a JSON object deep-merged over the APNs payload, e.g. --json "
                "'{\"ios\": {\"aps\": {\"interruption-level\": \"time-sensitive\"}}}'.")
    if name == "android":
        return ("Pass android as a JSON object of text values merged over the extras, e.g. "
                "--json '{\"android\": {\"gcm.notification.tag\": \"one\"}}'.")
    if name == "schema":
        return f"Leave schema out, or set it to {SCHEMA}."
    return MESSAGE_HINT


def _invalid(message: str, hint: str | None = None, **extra: Any) -> errors.AutonomError:
    if hint is None:
        field = extra.get("field")
        hint = _field_hint(field) if isinstance(field, str) else MESSAGE_HINT
    return errors.AutonomError(errors.INVALID_VALUE, message, hint, **extra)


def _fields_hint() -> str:
    return "Message fields: " + ", ".join(FIELDS) + ", and data.<key>. Other values: " \
        + ", ".join(ACTION_KEYS) + "."


def _boolean_words() -> dict[str, bool]:
    from .simulator import _BOOLEAN_WORDS  # noqa: PLC0415 - simulator imports this module lazily

    return _BOOLEAN_WORDS


def parse_boolean(key: str, value: Any) -> bool:
    """A boolean from JSON or from `--value` text; anything else is refused."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        words = _boolean_words()
        word = value.strip().lower()
        if word in words:
            return words[word]
    raise _invalid(f"{key} must be true or false",
                   "Booleans: " + ", ".join(sorted(_boolean_words())) + ".", field=key)


def _badge(value: Any) -> int:
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise _invalid("badge must be a whole number 0 or more",
                           "Pass --value badge=3.", field="badge") from None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid("badge must be a whole number 0 or more", "Pass --value badge=3.",
                       field="badge")
    return value


def _text(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} must be non-empty text", field=key)
    if "\x00" in value:
        raise _invalid(f"{key} must not contain a NUL character", field=key)
    if key == "message_id" and (value != value.strip() or len(value.split()) != 1):
        # it names the shade notification's tag, which a read-back matches
        raise _invalid("message_id must be one word without spaces", field=key)
    return value


def _path(key: str, value: Any, hint: str) -> Path:
    """A user path, refused with `invalid_value` rather than an exception
    (`~nosuchuser/...`, a NUL byte)."""
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} needs a path", hint, field=key)
    if "\x00" in value:
        raise _invalid(f"{key} must not contain a NUL character", hint, field=key)
    try:
        return Path(value.strip()).expanduser()
    except (RuntimeError, ValueError, OSError):
        raise _invalid(f"{key} names a home directory that does not exist", hint,
                       field=key) from None


def _object(key: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{key} must be a JSON object", field=key)
    return value


def reserved_data_key(key: str) -> bool:
    return key in RESERVED_DATA_KEYS or key.startswith(RESERVED_DATA_PREFIXES)


def _data(value: Any) -> dict[str, Any]:
    data = _object("data", value)
    for key, item in data.items():
        if not isinstance(key, str) or not key:
            raise _invalid("every data key must be non-empty text", field="data")
        if "\x00" in key or (isinstance(item, str) and "\x00" in item):
            raise _invalid(f"data.{key} must not contain a NUL character", field=f"data.{key}")
        if reserved_data_key(key):
            raise _invalid(
                f"data key {key!r} is reserved",
                "APNs and FCM reserve " + ", ".join(RESERVED_DATA_KEYS) + " and every key "
                "starting with " + " or ".join(RESERVED_DATA_PREFIXES) + "; rename it. The key "
                "an app reads a deep link from is app-specific (link, url, route, ...).",
                field=f"data.{key}")
    return dict(data)


def _android(value: Any) -> dict[str, str]:
    extras = _object("android", value)
    for key, item in extras.items():
        if not key or not isinstance(item, str):
            raise _invalid("android must map non-empty keys to text (FCM extras are strings)",
                           field="android")
        if "\x00" in key or "\x00" in item:
            raise _invalid("android extras must not contain a NUL character", field="android")
    return dict(extras)


def normalize(raw: Any) -> dict[str, Any]:
    """The checked message: known fields only, typed, `schema` first."""
    if not isinstance(raw, dict):
        raise _invalid("a push message must be a JSON object", MESSAGE_HINT)
    unknown = [key for key in raw if key not in FIELDS]
    if unknown:
        raise _invalid(f"unknown push message field {unknown[0]!r}", _fields_hint(),
                       field=unknown[0], valid_fields=list(FIELDS))
    message: dict[str, Any] = {"schema": SCHEMA}
    for key in FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "schema":
            if value != SCHEMA:
                raise _invalid(f"schema must be {SCHEMA!r}", field="schema")
            continue
        if key in TEXT_FIELDS:
            value = _text(key, value)
        elif key in LABEL_FIELDS:
            if not isinstance(value, str):
                raise _invalid(f"{key} must be text", field=key)
        elif key == "badge":
            value = _badge(value)
        elif key in BOOL_FIELDS:
            value = parse_boolean(key, value)
        elif key == "data":
            value = _data(value)
            if not value:
                continue  # an empty data object says nothing
        elif key == "ios":
            value = dict(_object("ios", value))
        elif key == "android":
            value = _android(value)
        message[key] = value
    if message.get("silent") is True and any(name in message for name in ALERT_FIELDS):
        raise _invalid("a silent message carries no title, subtitle or body",
                       "Drop silent=true, or drop the title, subtitle and body.",
                       field="silent")
    if not (any(name in message for name in ("title", "body", "data"))
            or message.get("silent") is True):
        raise _invalid("a push message needs at least one of title, body, data or silent=true",
                       MESSAGE_HINT, missing=["title", "body", "data", "silent"])
    return message


def new_message_id() -> str:
    return f"autonom-{int(time.time() * 1000)}-{secrets.token_hex(3)}"


def message_id_of(message: dict[str, Any]) -> str:
    """The message's own id, else a new one (never stored in the message)."""
    return message.get("message_id") or new_message_id()


# --- rendering ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def render_ios(message: dict[str, Any], message_id: str) -> dict[str, Any]:
    """The APNs payload: `aps` first, data keys at the top level (JSON types
    kept), then the escape hatch `ios` deep-merged over it."""
    aps: dict[str, Any] = {}
    if "title" in message or "subtitle" in message:
        aps["alert"] = {name: message[name] for name in ALERT_FIELDS if name in message}
    elif "body" in message:
        aps["alert"] = message["body"]
    if "badge" in message:
        aps["badge"] = message["badge"]
    if "sound" in message:
        aps["sound"] = message["sound"]
    if "category" in message:
        aps["category"] = message["category"]
    if "thread" in message:
        aps["thread-id"] = message["thread"]
    if "image" in message:
        aps["mutable-content"] = 1
    if message.get("silent") is True:
        aps["content-available"] = 1
    payload: dict[str, Any] = {"aps": aps}
    for key, value in (message.get("data") or {}).items():
        payload[key] = value
    if "image" in message:
        payload["fcm_options"] = {"image": message["image"]}
    if message.get("fcm", True):
        # what makes a Firebase SDK (FlutterFire included) treat it as its own message
        payload["gcm.message_id"] = message_id
    if "ios" in message:
        payload = _deep_merge(payload, message["ios"])
    return payload


def payload_bytes(payload: dict[str, Any]) -> int:
    """The size APNs counts: UTF-8, compact separators."""
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def ios_kind(message: dict[str, Any]) -> str:
    return "notification" if any(name in message for name in ALERT_FIELDS) else "data"


def android_kind(message: dict[str, Any]) -> str:
    """A title or body makes an FCM *notification message*; anything else is data-only."""
    return "notification" if ("title" in message or "body" in message) else "data"


def _extra_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           separators=(",", ":"))


def render_android(message: dict[str, Any], message_id: str,
                   sent_time_ms: int | None = None) -> dict[str, str]:
    """The FCM extras (every value a string), as Google Play services sends
    them to an app's `com.google.android.c2dm.intent.RECEIVE` receiver."""
    extras: dict[str, str] = {}
    if android_kind(message) == "notification":
        extras["gcm.n.e"] = "1"
    for name, key in (("title", "gcm.notification.title"), ("body", "gcm.notification.body"),
                      ("badge", "gcm.notification.notification_count"),
                      ("sound", "gcm.notification.sound"),
                      ("category", "gcm.notification.click_action"),
                      ("channel", "gcm.notification.android_channel_id"),
                      ("image", "gcm.notification.image")):
        if name in message:
            extras[key] = str(message[name])
    for key, value in (message.get("data") or {}).items():
        extras[key] = _extra_text(value)
    sent = sent_time_ms if sent_time_ms is not None else int(time.time() * 1000)
    extras["google.message_id"] = message_id
    extras["google.sent_time"] = str(sent)
    extras["from"] = FCM_SENDER
    extras["google.ttl"] = FCM_TTL_SECONDS
    extras.update(message.get("android") or {})
    return extras


def notes(message: dict[str, Any], platform: str | None = None) -> list[str]:
    """Short lines on what one platform leaves out or changes."""
    ios: list[str] = []
    android: list[str] = []
    if "channel" in message:
        ios.append("ios: channel is not used (Android only)")
    if "android" in message:
        ios.append("ios: android is not used (Android extras)")
    for name in ("subtitle", "thread"):
        if name in message:
            android.append(f"android: {name} is not used (iOS only)")
    if "ios" in message:
        android.append("android: ios is not used (APNs payload)")
    if message.get("fcm") is False:
        android.append("android: fcm has no effect (an Android message always carries "
                       "google.message_id)")
    for key, value in (message.get("data") or {}).items():
        if not isinstance(value, str):
            android.append(f"android: data.{key} is sent as JSON text (extras are strings)")
    if android_kind(message) == "data":
        unused = [name for name in ("badge", "sound", "category", "channel", "image")
                  if name in message]
        if unused:
            android.append("android: " + ", ".join(unused) + " show nothing in a data-only "
                           "message (no title or body)")
    if platform == "ios":
        return ios
    if platform == "android":
        return android
    return ios + android


# --- command-line values and files -------------------------------------------------------


@dataclass
class PushInput:
    """What `--json`/`--value` asked for: a composed `message`, or a raw
    APNs `payload` (`payload=` or a file with a top-level `aps`)."""
    message: dict[str, Any] | None = None
    raw: bool = False
    payload: Any = None
    file: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


def load_file(path: Any) -> tuple[str, dict[str, Any], Path]:
    """("payload" | "message", the object, the path). A file whose object has
    a top-level `aps` key is a raw APNs payload."""
    resolved = _path("file", path, f"Pass --value file={FILE_CONVENTION}.")
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _invalid(f"push file {resolved} does not exist",
                       f"Save one with 'autonom simulator push compose ... --value "
                       f"write={FILE_CONVENTION}'.", path=str(resolved)) from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or exc.__class__.__name__
        raise _invalid(f"push file {resolved} cannot be read: {reason}",
                       "Pass a readable UTF-8 JSON file.", path=str(resolved)) from None
    try:
        content = json.loads(text)
    except ValueError as exc:
        where = (f" (line {exc.lineno}, column {exc.colno})"
                 if isinstance(exc, json.JSONDecodeError) else "")
        raise _invalid(f"push file {resolved} is not valid JSON{where}",
                       "A push file holds one JSON object.", path=str(resolved)) from None
    if not isinstance(content, dict):
        raise _invalid(f"push file {resolved} does not hold a JSON object",
                       "A push file holds one JSON object: a push message, or a raw APNs "
                       "payload with a top-level aps key.", path=str(resolved))
    return ("payload" if "aps" in content else "message"), content, resolved


def _command_line_field(key: str, value: Any) -> Any:
    """`--value` gives text: badge, booleans and objects are parsed here;
    `--json` values are already typed and are checked by `normalize`."""
    if not isinstance(value, str):
        return value
    if key == "badge":
        return _badge(value)
    if key in BOOL_FIELDS:
        return parse_boolean(key, value)
    if key in ("data", "ios", "android"):
        try:
            parsed = json.loads(value)
        except ValueError:
            raise _invalid(f"{key} must be a JSON object", field=key) from None
        return _object(key, parsed)
    return value


def from_values(values: dict[str, Any]) -> PushInput:
    """Split the values of one push action into the message (or raw
    payload) and the action's own values.

    `payload=` keeps the original `push send` path exactly: values that are
    neither message fields nor action values are ignored there, as before.
    Everywhere else an unknown value is refused, naming it.
    """
    fields: dict[str, Any] = {}
    data_keys: dict[str, Any] = {}
    options: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in values.items():
        if key in ACTION_KEYS:
            options[key] = value
        elif key.startswith("data."):
            name = key[len("data."):]
            if not name:
                raise _invalid("data.<key> needs a key name", "Pass --value data.order_id=42.",
                               field=key)
            data_keys[name] = value
        elif key in FIELDS:
            fields[key] = value
        else:
            unknown.append(key)
    composing = bool(fields or data_keys)
    if "payload" in options:
        if composing or "file" in options:
            raise _invalid("payload (a raw APNs payload) cannot be combined with message fields "
                           "or a file", "Send either payload=<APNs JSON> or message fields "
                           "(title, body, data, ...), not both.",
                           field="payload")
        payload = options["payload"]
        if isinstance(payload, str):
            # `--value payload={...}` arrives as text.
            try:
                payload = json.loads(payload)
            except ValueError:
                pass
        return PushInput(raw=True, payload=payload, options=options)
    if unknown:
        raise _invalid(f"unknown push value {unknown[0]!r}", _fields_hint(), field=unknown[0],
                       valid_fields=list(FIELDS), valid_values=list(ACTION_KEYS))
    base: dict[str, Any] = {}
    file_path: str | None = None
    if "file" in options:
        kind, content, resolved = load_file(options["file"])
        file_path = str(resolved)
        if kind == "payload":
            if composing:
                raise _invalid(f"push file {resolved} holds a raw APNs payload (a top-level aps "
                               "key); message fields cannot be laid over it",
                               "Drop the message fields, or save a push message with "
                               "'autonom simulator push compose'.", path=file_path)
            return PushInput(raw=True, payload=content, file=file_path, options=options)
        base = content
    merged = dict(base)
    for key, value in fields.items():
        parsed = _command_line_field(key, value)
        if key == "data" and isinstance(parsed, dict) and isinstance(merged.get("data"), dict):
            parsed = {**merged["data"], **parsed}
        merged[key] = parsed
    if data_keys:
        current = merged.get("data", {})
        if not isinstance(current, dict):
            raise _invalid("data must be a JSON object", field="data")
        merged["data"] = {**current, **data_keys}
    if not merged and file_path is None:
        return PushInput(options=options)  # nothing was given at all
    return PushInput(message=normalize(merged), file=file_path, options=options)


def refuse_other_values(options: dict[str, Any], allowed: tuple[str, ...], verb: str) -> None:
    """An action value of another push action is refused, never ignored."""
    for key in options:
        if key not in allowed:
            raise _invalid(f"push {verb} does not take {key}",
                           f"push {verb} values: " + ", ".join(allowed)
                           + ", plus the message fields.", field=key)


# --- compose ------------------------------------------------------------------------------


def _write_message(path: Path, message: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(message, ensure_ascii=False, indent=2) + "\n")
            os.chmod(temp, 0o644)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"cannot write the push file {path}: {exc.strerror or exc.__class__.__name__}",
            "Pass write=<path> in a writable directory.", path=str(path)) from exc


def _write_target(options: dict[str, Any]) -> Path | None:
    write = options.get("write")
    if write is None:
        return None
    out = _path("write", write, f"Pass --value write={FILE_CONVENTION}.")
    try:
        is_dir, exists = out.is_dir(), out.exists()
    except OSError as exc:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"cannot check the push file {out}: {exc.strerror or exc.__class__.__name__}",
            "Pass write=<path> in a writable directory.", path=str(out)) from None
    if is_dir:
        raise _invalid(f"{out} is a directory", "Pass write=<path to a .json file>.",
                       path=str(out))
    overwrite = parse_boolean("overwrite", options.get("overwrite", False))
    if exists and not overwrite:
        raise _invalid(f"{out} already exists and was left as it is",
                       "Pass --value overwrite=true to replace it, or another write=<path>.",
                       path=str(out))
    return out


def compose(values: dict[str, Any]) -> dict[str, Any]:
    """`simulator push compose`: the message and what each platform gets.
    Read-only (it writes only the file `write=` names) and needs no device."""
    push = from_values(values)
    refuse_other_values(push.options, COMPOSE_KEYS, "compose")
    if push.raw:
        raise _invalid("push compose builds a push from message fields; a raw APNs payload "
                       "is sent as it is", "Send it with 'autonom simulator push send --value "
                       "payload=...' or --value file=<payload.json>, or describe the push with "
                       "message fields.", field="payload" if push.file is None else "file")
    if push.message is None:
        raise _invalid("push compose needs message fields", MESSAGE_HINT,
                       missing=["title", "body", "data", "silent"])
    platform = push.options.get("platform")
    if platform is not None and platform not in PLATFORMS:
        raise _invalid("platform must be ios or android", "Leave it out for both.",
                       field="platform")
    out = _write_target(push.options)
    message = push.message
    message_id = message_id_of(message)
    reply: dict[str, Any] = {"control": "push", "action": "compose", "message": message,
                             "message_id": message_id}
    warnings: list[dict[str, Any]] = []
    if platform in (None, "ios"):
        payload = render_ios(message, message_id)
        size = payload_bytes(payload)
        reply["ios"] = {"payload": payload, "bytes": size,
                        "over_limit": size > IOS_PAYLOAD_LIMIT}
        if size > IOS_PAYLOAD_LIMIT:
            warnings.append(large_payload_warning(size))
    if platform in (None, "android"):
        reply["android"] = {"extras": render_android(message, message_id),
                            "kind": android_kind(message)}
    reply["notes"] = notes(message, platform)
    reply["warnings"] = warnings
    if out is not None:
        _write_message(out, message)
    reply["written"] = str(out) if out is not None else None
    reply["next"] = ("autonom simulator push send --value " + shlex.quote(f"file={out}")
                     if out is not None else None)
    return reply


def large_payload_warning(size: int) -> dict[str, Any]:
    return {"code": "push_payload_large",
            "error": f"the APNs payload is {size} bytes, above the {IOS_PAYLOAD_LIMIT}-byte "
                     "limit APNs enforces",
            "hint": "Shorten the body or the data; simctl may still accept it, a real APNs "
                    "send would not.",
            "bytes": size, "limit": IOS_PAYLOAD_LIMIT}
