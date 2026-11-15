"""`simulator push send` on an Android emulator: every adb command line of it.

Two deliveries:

- ``app`` (default) hands the message to the app's own push receiver the way
  Google Play services hands it an FCM message: one ordered broadcast of
  ``com.google.android.c2dm.intent.RECEIVE`` to the package, the FCM fields
  as string extras. Only a privileged sender reaches that receiver: a shell
  broadcast is silently dropped (``result=0``), so it needs root — adbd
  running as root (``id -u`` is 0) or ``su 0`` (google_apis images; Play
  Store images have neither). ``adb root`` is never run: it restarts adbd and
  drops every forward. A custom ``receiver=``/``action=`` is sent as root
  when root is there, else as the shell.
- ``shade`` posts a plain notification with ``cmd notification post`` as the
  shell (API 29+). The app's code does not run and the notification belongs
  to ``com.android.shell``; a tap opens nothing. It is never posted through
  ``su``: a root-posted notification is not shown.

Every text argument that reaches ``adb shell`` is quoted with
``simulator.shell_quote`` (adb joins its argv into one ``sh`` line), and
``su 0`` passes the words it was given straight to the command. No error or
reply here quotes the notification text: device output of a command that
carried it is never copied into an error.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from . import adb as adb_mod
from . import device_state, errors, push_message
from .platform import Target

RECEIVE_ACTION = "com.google.android.c2dm.intent.RECEIVE"
SHELL_PACKAGE = "com.android.shell"
POST_NOTIFICATIONS = "android.permission.POST_NOTIFICATIONS"
SHADE_MIN_API = 29        # `cmd notification post`
NOTIFICATION_PERMISSION_API = 33  # POST_NOTIFICATIONS is a runtime permission from here
READ_BACK_SECONDS = 3.0   # how long the shade is polled for the record
READ_BACK_INTERVAL = 0.3
TAG_PREFIX = "autonom_push_"
APP_KEYS = ("app_id", "delivery", "receiver", "action", "include_stopped", "file")
SHADE_KEYS = ("app_id", "delivery", "file")

_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
_COMPONENT = re.compile(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+")
_ACTION = re.compile(r"[A-Za-z0-9_.]+")
_RESULT = re.compile(r"Broadcast completed: result=(-?\d+)")
_RECORD_FIELD = {name: re.compile(rf"\b{name}=(\S+)") for name in ("pkg", "user", "id", "tag")}
_EXTRA_LINE = re.compile(r"^\s*android\.(title|text)=[A-Za-z]+ \((.*)$")

NO_ROOT_HINT = (
    "Delivering into the app's push receiver needs root, and Play Store emulator images have "
    "none (no su): use a google_apis emulator image (not google_apis_playstore). Other ways: "
    "receiver=<package>/<class> or action=<intent action> for a receiver of your own, or "
    "delivery=shade for a plain notification in the shade.")


def _not_delivered(reason: str, message: str, hint: str, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(errors.PUSH_NOT_DELIVERED, message, hint, reason=reason,
                               capability="simulator.push", **extra)


def _shell(target: Target, words: list[str], *, timeout: float = 30) -> tuple[int, str]:
    """One `adb shell` line: (exit code, output). A timeout or a missing adb
    is an error naming the command only, never its arguments."""
    try:
        completed = adb_mod.run_adb(target.tool, ["shell", *words], serial=target.target_id,
                                    timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        name = " ".join(word for word in words[:3] if not word.startswith("'"))
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"adb shell {name} did not answer within {int(timeout)} s",
            "Check the emulator with 'adb devices'.") from None
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    return completed.returncode, text


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def api_level(target: Target) -> int | None:
    code, text = _shell(target, ["getprop", "ro.build.version.sdk"], timeout=15)
    word = text.strip()
    return int(word) if code == 0 and word.isdigit() else None


def require_installed(target: Target, package: str) -> None:
    """`pm path <pkg>` prints `package:<apk>` (exit 0); nothing and exit 1
    when the package is not installed. Any other answer is a device problem."""
    code, text = _shell(target, ["pm", "path", device_quote(package)])
    if code == 0 and any(line.strip().startswith("package:") for line in text.splitlines()):
        return
    if code != 0 and not text.strip():
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED, f"{package} is not installed on the device",
            "Install it first, or check the package id with 'adb shell pm list packages'.",
            app_id=package)
    raise errors.AutonomError(
        errors.BACKEND_FAILED,
        f"pm path {package} failed (exit {code}): {_last_line(text) or 'no output'}",
        "Check the emulator with 'adb devices'; it must be online.")


def root_route(target: Target) -> str | None:
    """"root" when adbd runs as root, "su" when `su 0` gives uid 0, else
    None. Any other output or a non-zero exit of `su 0 id -u` is no root
    (a Play Store image prints `su: inaccessible or not found`, exit 127)."""
    code, text = _shell(target, ["id", "-u"], timeout=15)
    if code != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"id -u failed on the device (exit {code}): {_last_line(text) or 'no output'}",
            "Check the emulator with 'adb devices'; it must be online.")
    # the last line: adb may print its own banner first ("* daemon started ...")
    if _last_line(text) == "0":
        return "root"
    code, text = _shell(target, ["su", "0", "id", "-u"], timeout=15)
    if code == 0 and _last_line(text) == "0":
        return "su"
    return None


def fcm_receivers(target: Target, package: str) -> list[str]:
    """The package's receivers for the FCM action (`<pkg>/<class>` lines)."""
    code, text = _shell(target, ["cmd", "package", "query-receivers", "--brief", "-a",
                                 RECEIVE_ACTION, device_quote(package)])
    if code != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"cmd package query-receivers failed (exit {code}): {_last_line(text) or 'no output'}",
            "Check the emulator with 'adb devices'; it must be online.")
    prefix = package + "/"
    return [line.strip() for line in text.splitlines()
            if line.strip().startswith(prefix) and _COMPONENT.fullmatch(line.strip())]


def notification_records(target: Target) -> list[dict[str, Any]] | None:
    """The live notification records from `dumpsys notification --noredact`
    (`pkg`, `user`, `id`, `tag`, `title`, `text`), or None when it could not
    be read. `title`/`text` are the rest of their line after `Type (`."""
    code, text = _shell(target, ["dumpsys", "notification", "--noredact"], timeout=15)
    if code != 0:
        return None
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if "NotificationRecord(" in line:
            current = {name: (match.group(1) if (match := pattern.search(line)) else None)
                       for name, pattern in _RECORD_FIELD.items()}
            current["title"] = current["text"] = None
            records.append(current)
            continue
        if current is None:
            continue
        match = _EXTRA_LINE.match(line)
        if match and current[match.group(1)] is None:
            current[match.group(1)] = match.group(2).rstrip()
    return records


def _identity(record: dict[str, Any]) -> tuple[Any, ...]:
    return (record.get("pkg"), record.get("user"), record.get("id"), record.get("tag"))


def _shows(line: str | None, expected: str) -> bool:
    """Whether a dumped `android.title=String (<line>` holds `expected`.
    A one-line value ends with `)` on the same line; a multi-line one is
    compared on its first line."""
    if line is None:
        return False
    if "\n" in expected:
        return line == expected.split("\n", 1)[0]
    return line == expected + ")"


def device_quote(text: str) -> str:
    from .simulator import shell_quote  # noqa: PLC0415 - simulator imports this module lazily

    return shell_quote(text)


def _boolean(options: dict[str, Any], key: str) -> bool:
    return push_message.parse_boolean(key, options[key]) if key in options else False


def _delivery(options: dict[str, Any]) -> str:
    delivery = options.get("delivery", "app")
    if delivery not in push_message.DELIVERIES:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "delivery must be app or shade",
            "app (default) delivers into the app's push receiver; shade shows a plain "
            "notification without the app.", field="delivery")
    return delivery


def _notifications_warning(target: Target, package: str, api: int | None) -> dict[str, Any] | None:
    """`push_notifications_denied` when the app holds no POST_NOTIFICATIONS on
    API 33+; nothing when the level or the permission cannot be read."""
    if api is None or api < NOTIFICATION_PERMISSION_API:
        return None
    try:
        states = device_state.app_runtime_permissions(target, package)
    except Exception:  # noqa: BLE001 - a failed lookup means no warning
        return None
    entry = next((item for item in states if item.get("name") == POST_NOTIFICATIONS), None)
    if entry is None or entry.get("granted") is not False:
        return None
    return {"code": "push_notifications_denied",
            "error": f"{package} has not been granted {POST_NOTIFICATIONS} (API {api}): the "
                     "message reaches the app, but a notification it posts is not shown",
            "hint": f"autonom permissions grant notifications {package}",
            "permission": POST_NOTIFICATIONS}


def _records_or_none(target: Target) -> list[dict[str, Any]] | None:
    """A read of the shade that never fails the push: it was sent already
    (or is about to be), so a read that times out only loses the read-back."""
    try:
        return notification_records(target)
    except errors.AutonomError:
        return None


def _poll(target: Target, found: Any) -> bool:
    deadline = time.monotonic() + READ_BACK_SECONDS
    while True:
        records = _records_or_none(target)
        if records is not None and any(found(record) for record in records):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(READ_BACK_INTERVAL)


def send(target: Target, push: push_message.PushInput, app_id: str,
         app_id_source: str | None) -> dict[str, Any]:
    if push.raw:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "a raw APNs payload is iOS only",
            "On Android describe the push with message fields: --value title=Hello --value "
            "body=... --value data.<key>=<text> (see 'autonom simulator push compose').",
            field="payload" if push.file is None else "file")
    delivery = _delivery(push.options)
    push_message.refuse_other_values(push.options, SHADE_KEYS if delivery == "shade" else APP_KEYS,
                                     f"send (Android, delivery={delivery})")
    if push.message is None:
        raise errors.AutonomError(errors.INVALID_VALUE, "push send needs message fields",
                                  push_message.MESSAGE_HINT,
                                  missing=["title", "body", "data", "silent"])
    if delivery == "shade":
        return _shade(target, push.message, app_id, app_id_source)
    return _app(target, push.message, push.options, app_id, app_id_source)


def _app(target: Target, message: dict[str, Any], options: dict[str, Any], app_id: str,
         app_id_source: str | None) -> dict[str, Any]:
    if not app_id:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "push send needs app_id",
            "Pass --value app_id=<package>, or start a session with --app-id on this "
            "emulator.", missing=["app_id"])
    if not _PACKAGE.fullmatch(app_id):
        raise errors.AutonomError(errors.INVALID_VALUE, f"not an Android package name: {app_id!r}",
                                  "Pass a package id such as com.example.app.", field="app_id")
    receiver = options.get("receiver")
    action = options.get("action")
    if receiver is not None:
        if not isinstance(receiver, str) or not receiver.strip():
            raise errors.AutonomError(errors.INVALID_VALUE, "receiver must be a component name",
                                      "Pass receiver=<package>/<class> or receiver=.<Class>.",
                                      field="receiver")
        receiver = receiver.strip()
        if "/" not in receiver:
            receiver = f"{app_id}/{receiver}"
        if not _COMPONENT.fullmatch(receiver):
            raise errors.AutonomError(errors.INVALID_VALUE, f"not a component name: {receiver!r}",
                                      "Pass receiver=<package>/<class>.", field="receiver")
    if action is not None and (not isinstance(action, str) or not _ACTION.fullmatch(action)):
        raise errors.AutonomError(errors.INVALID_VALUE, "action must be an intent action name",
                                  "Pass action=com.example.PUSH.", field="action")
    include_stopped = _boolean(options, "include_stopped")
    explicit = receiver is not None or action is not None
    message_id = push_message.message_id_of(message)
    extras = push_message.render_android(message, message_id)
    kind = push_message.android_kind(message)

    require_installed(target, app_id)
    route = root_route(target)
    if route is None:
        if not explicit:
            raise _not_delivered(
                "no_root", f"no root on {target.target_id}: a broadcast from the shell does not "
                           f"reach {app_id}'s push receiver", NO_ROOT_HINT, app_id=app_id)
        route = "shell"
    if not explicit and not fcm_receivers(target, app_id):
        raise _not_delivered(
            "no_receiver",
            f"{app_id} has no receiver for {RECEIVE_ACTION} (no Firebase Messaging in the app?)",
            "Pass receiver=<package>/<class> or action=<intent action> to send to a receiver "
            "of your own, or delivery=shade for a plain notification in the shade.",
            app_id=app_id)
    warnings: list[dict[str, Any]] = []
    # what the app's records showed before the send: a record counts as this
    # push's only when it is new, or when a replaced one shows other text
    before: dict[tuple[Any, ...], tuple[Any, Any]] = {}
    if kind == "notification":
        warning = _notifications_warning(target, app_id, api_level(target))
        if warning:
            warnings.append(warning)
        before = {_identity(record): (record.get("title"), record.get("text"))
                  for record in (_records_or_none(target) or []) if record.get("pkg") == app_id}

    words = ["su", "0"] if route == "su" else []
    words += ["am", "broadcast"]
    if include_stopped:
        words.append("--include-stopped-packages")
    words += ["-a", device_quote(action or RECEIVE_ACTION)]
    words += ["-n", device_quote(receiver)] if receiver else ["-p", device_quote(app_id)]
    for key, value in extras.items():
        words += ["--es", device_quote(key), device_quote(value)]
    code, output = _shell(target, words)
    match = _RESULT.search(output)
    if match is None:
        refused = " (Permission Denial)" if "Permission Denial" in output else ""
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"am broadcast printed no result line (exit {code}){refused}",
            "Check the emulator with 'adb devices', and the package with 'adb shell pm path "
            f"{app_id}'.", app_id=app_id)
    result = int(match.group(1))
    if result == 0 and not explicit:
        raise _not_delivered(
            "not_received", f"{app_id} did not receive the push (broadcast result=0)",
            "The app is stopped or was never started: launch it once (autonom session launch) "
            "or pass include_stopped=true. A custom receiver must set a result.",
            app_id=app_id, receiver_result=0)
    if result not in (-1, 0):
        raise _not_delivered(
            "rejected", f"{app_id}'s receiver answered result={result}",
            f"The app's receiver refused the message; read its log with 'autonom logs tail "
            f"--package {app_id}'.", app_id=app_id, receiver_result=result)

    reply: dict[str, Any] = {
        "control": "push", "app_id": app_id, "app_id_source": app_id_source,
        "composed": True, "message_id": message_id, "kind": kind, "delivery": "app",
        "route": route, "receiver_result": result, "extras_count": len(extras),
    }
    if explicit:
        reply["receiver"] = receiver
        reply["intent_action"] = action or RECEIVE_ACTION
    if kind == "notification":
        expected = message.get("title") or message.get("body")
        field = "title" if "title" in message else "text"

        def found(record: dict[str, Any]) -> bool:
            if record.get("pkg") != app_id or not _shows(record.get(field), expected):
                return False
            earlier = before.get(_identity(record))
            return earlier is None or earlier != (record.get("title"), record.get("text"))

        if _poll(target, found):
            reply.update(verified=True, verification="read_back")
        else:
            # a foreground app handles the message itself and shows nothing
            reply.update(verified=False, verification="not_in_shade")
    else:
        reply.update(verified=False, verification="receiver_accepted")
    if warnings:
        reply["warnings"] = warnings
    return reply


def _shade(target: Target, message: dict[str, Any], app_id: str | None,
           app_id_source: str | None) -> dict[str, Any]:
    title = message.get("title")
    text = message.get("body") or title
    if not text:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "delivery=shade needs a title or a body",
            "A data-only message has nothing to show; use delivery=app.",
            missing=["title", "body"])
    api = api_level(target)
    if api is not None and api < SHADE_MIN_API:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            f"delivery=shade needs API {SHADE_MIN_API} or newer (cmd notification post); "
            f"this emulator is API {api}",
            "Use delivery=app, or an emulator image with API 29 or newer.",
            capability="simulator.push", api_level=api)
    message_id = push_message.message_id_of(message)
    tag = TAG_PREFIX + message_id
    # As the shell, never through su: a notification posted as root is not shown.
    words = ["cmd", "notification", "post", "-S", "bigtext"]
    if title:
        words += ["-t", device_quote(title)]
    words += [device_quote(tag), device_quote(text)]
    code, _output = _shell(target, words)
    if code != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"cmd notification post failed (exit {code})",
            "Check the emulator with 'adb devices'.")
    reply: dict[str, Any] = {
        "control": "push", "app_id": app_id or None, "app_id_source": app_id_source if app_id else None,
        "composed": True, "message_id": message_id, "kind": "notification",
        "delivery": "shade", "owner": SHELL_PACKAGE, "tag": tag,
    }
    if _poll(target, lambda record: record.get("pkg") == SHELL_PACKAGE and record.get("tag") == tag):
        reply.update(verified=True, verification="read_back")
    else:
        reply.update(verified=False, verification="not_in_shade",
                     verification_detail="no com.android.shell record with this tag within "
                                         f"{READ_BACK_SECONDS:g} s; a shell whose adbd runs as "
                                         "root posts notifications that are not shown")
    reply["warnings"] = [{
        "code": "push_shade_only",
        "error": "the notification belongs to com.android.shell: the app's code did not run "
                 "and a tap opens nothing",
        "hint": "Use delivery=app (the default) to test how the app handles the push.",
    }]
    return reply
