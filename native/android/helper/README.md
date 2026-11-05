# Autonom Android helper (protocol 1)

A small Java program that runs on the device as the shell user, holds one
UiAutomation connection, and answers JSON lines on the abstract local socket
`autonom-helper`. It is Autonom's own code and uses only Android framework
APIs (`org.json` from the platform for JSON).

Only the source is in the repository and the release. The host builds it on
first use with the local JDK and Android SDK and caches the result under the
Autonom state directory: see `scripts/autonom_lib/android_helper_build.py`.
`*.dex` and `*.class` are ignored by git.

## Files

| File | Role |
| --- | --- |
| `Main.java` | options, the socket server, request ids, the idle exit, dispatch |
| `Automation.java` | the UiAutomation connection (by reflection), window tracking, event times, settle, display |
| `TreeXml.java` | `uiautomator dump`-compatible XML of the active window, the windows list |
| `Input.java` | touches, keys, typing and set-text |
| `Shot.java` | JPEG screenshots scaled to a long edge |
| `HelperError.java` | a failure with a protocol error code |

## Start

```sh
adb -s SERIAL push classes.dex /data/local/tmp/autonom-helper-<build>.dex
adb -s SERIAL shell "CLASSPATH=/data/local/tmp/autonom-helper-<build>.dex nohup app_process /system/bin \
  --nice-name=autonom-helper dev.autonom.helper.Main --build <build> --idle-ms 120000 \
  --socket autonom-helper >/data/local/tmp/autonom-helper.log 2>&1 &"
adb -s SERIAL forward tcp:0 localabstract:autonom-helper   # prints the host port
```

The log holds start and exit lines only, never request content. A second
helper started while one serves the socket exits at once (`exit
socket_in_use`) and never touches UiAutomation. The host client then goes by
the `hello` of the helper that serves the socket: one of the expected build
serves the call; one that answers at once with another build or protocol is
idle and is replaced (`quit`, a `kill` of its pid only if it stays and
`/proc/<pid>/cmdline` still names `autonom-helper`, then a new start, once
per start); one whose `hello` stays queued behind another connection's
request for the whole 5 s start budget is busy and is left running
(`helper_busy`, `helper_foreign_pids`).

## Protocol

One UTF-8 JSON object per line. Request `{"id": "<1-64 chars>", "op": "...", ...}`;
reply `{"id": "<same>", "ok": true, ...}` or
`{"id": "<same>", "ok": false, "error": {"code": "...", "message": "..."}}`
(`id` is null when the request had no usable id).

- The first request of every connection must be `hello` (else `hello_required`).
- The last 256 ids are kept for the whole process; a repeated id is refused
  with `duplicate_id` and not performed, whatever its arguments.
- One connection is served at a time; others wait. A connection that sends
  nothing for 30 s (or for the idle time, when shorter) is closed.
- After `--idle-ms` without a request the helper releases UiAutomation and
  exits (`exit idle`).

| op | arguments | reply |
| --- | --- | --- |
| `hello` | — | `protocol`, `build`, `pid`, `sdk_int`, `uiautomation: "connected"` |
| `read` | `quiet_ms` 500, `max_ms` 2000 | `xml`, `settled`, `settle_ms`, `busy` (not settled only), `windows`, `display` |
| `display` | — | `width`, `height`, `rotation` |
| `tap` | `x`, `y` | `performed` + settle block |
| `long_press` | `x`, `y`, `duration_ms` 1000 | `performed` + settle block |
| `swipe` | `x1`, `y1`, `x2`, `y2`, `duration_ms` 300 | `performed` + settle block |
| `key` | `keycode` (int) | `performed` + settle block |
| `type` | `text` | `performed`, `method` (`keys`, `set_text`, `mixed`), `text_after`, `complete` |
| `set_text` | `text`, `expect_bounds` [l,t,r,b] | `performed`, `text_after`, `complete` |
| `screenshot` | `max_edge` 1000, `quality` 80 | `format: "jpeg"`, `width`, `height`, `screen_width`, `screen_height`, `data_base64` |
| `quit` | — | ok, then the process exits |

**Settle block.** `tap`, `long_press`, `swipe` and `key` settle by default.
Every limit counts from the end of the input, and the whole settle ends
within `max_ms` (2000) of it. First the helper waits until the app has
handled the input: one accessibility round trip to the app of the active
window comes back only after the work the input queued on the app's main
thread (Android sends the click event after a click handler returns, and a
handler that starts an activity can take longer than 300 ms on a busy
device). The round trip runs on its own thread and is waited for at most
until `max_ms`; an app that has not answered by then is not settled (the
system itself would wait about 5 s for it). From the answer the helper lets
a window transition finish (the platform's own `syncInputTransactions`,
given at most until `max_ms` minus `quiet_ms`), waits up to
`first_event_ms` (300) for the first accessibility event caused by the
input, then until `quiet_ms` (500) passed without events, counted from the
app's answer at the earliest (an input that caused no event waits
`quiet_ms` after the answer). The reply adds `settled` (quiet reached),
`settle_ms` and `first_event_ms`, both counted from the end of the input
(`first_event_ms` is 0 when the event came during the injection, null when
none came by the end of the first-event wait), and, only when `settled` is
false, `busy` (below).
`settle: {first_event_ms, quiet_ms, max_ms}` changes the limits;
`settle: false` skips the wait and the round trip. `type` and `set_text`
settle only when the request has `settle` or `read_after`. With
`read_after: true` the reply adds `read: {xml, windows, display}`; when that
read fails after the input was performed, the reply stays `ok` with
`performed: true` and carries `read_error: {code, message}` instead. An
error reply to an input means it was not performed, or that it failed part
way while being injected (`inject_failed`, `internal`); it is never an input
that completed. A `type` that fails after part of its text is in answers
`ok` with `complete: false` instead (see Typing).

**read.** Waits until `quiet_ms` passed since the last accessibility event
(at most `max_ms`), then reads like `read_after`; `busy` when not settled.

**busy.** What kept a settle or a read from quiet: the accessibility events
that arrived during its wait and counted against quiet (the window changes
of the helper's own tracking switches never count), as
`{"events": 37, "last": [{"package": "com.android.systemui", "class":
"android.widget.FrameLayout", "type": "TYPE_WINDOW_CONTENT_CHANGED", "ms":
1980}, ...], "packages": {"com.android.systemui": 30, "com.android.settings":
7}}`: the count, the last 8 events with their arrival in ms from the same
origin as `settle_ms`, and the count per package, most first (`""` for
events without a package). A settle whose app never answered the round trip
after the input also has `"app_answered": false`. Reporting only: quiet is
the same rule either way.

**XML.** The active window only, as `uiautomator dump` writes it: the same
serializer, attributes and order (`NAF` when uiautomator would add it, then
`index text resource-id class package content-desc checkable checked
clickable enabled focusable focused scrollable long-clickable password
selected bounds drawing-order hint`), bounds clipped to the display, and
invisible children skipped with their index kept. The tree is read with
window tracking (`FLAG_RETRIEVE_INTERACTIVE_WINDOWS`) switched off, as
`uiautomator dump` reads it: with tracking on, Android marks every node
outside the part of its window that no other window covers as invisible
(for example a list row with empty bounds at the edge of the navigation
bar), so those nodes would be missing. Tracking is on for everything else
(the windows list, input focus); the windows-changed events that switching
it causes do not count as screen changes.

**Windows.** Every window the device reports: `id`, `type` (`application`,
`input_method`, `system`, `accessibility_overlay`, `split_screen_divider`,
`magnification_overlay`, `unknown`), `type_id`, `layer`, `active`,
`focused`, `title`, `package`, `bounds` [l,t,r,b].

**Typing.** The text is sent in parts: a run of characters the virtual
keyboard map produces goes as key events; a run of the others goes as one
set-text of the whole expected text (the text before and after the
selection kept, the cursor put back behind the insert). Everything is
decided before the first event: the parts, and a refusal with
`text_not_typable` when the text needs set-text and the field is a password
field whose text cannot be read (an empty one, known by its hint or an
empty text, gets the whole text in one set-text). Any other field is typed
into as it reads; one whose node has no text at all (an empty field without
a hint reports none) counts as empty (owner decision ODA-002; a custom field
that hides its text from accessibility could have that text replaced). Each part must show up in
the field before the next one is sent, within 1500 ms + 60 ms per character
of the part (at most 5000 ms), and all of a `type`'s waits end 6000 ms after
it started. Key events that follow a set-text wait first until no
accessibility event came for 200 ms after the set-text (at most 1000 ms): an
app may restart the keyboard's input connection when its text changes (the
Settings search field does when it stops being empty), and a key the
keyboard handles itself, such as a space, sent during that restart is lost.
When a part has not shown up by the end of its wait, nothing more is sent
(nothing is ever sent twice), and the reply is still `ok` with
`performed: true`; so is a reply whose later part the device refused after
an earlier part was in (an error reply means nothing reached the field):

- `text_after`: what the field shows after typing; null when it cannot be
  read (a password field, a field that exposes no text, or one that went
  away).
- `complete`: true when the field shows the expected text; false when a part
  did not show up in time, or was refused, and typing stopped there; null
  when every part was sent and the text cannot be read.

`set_text` waits the same way for its text and reports `text_after` and
`complete` the same way (`complete` false when the field shows other text,
for example a field that limits or formats its input).

**Error codes.** `bad_request`, `hello_required`, `duplicate_id`,
`unknown_op`, `uiautomation_busy` (another tool holds UiAutomation),
`uiautomation_failed`, `no_focus`, `not_editable`, `text_not_typable`,
`fill_target_mismatch`, `inject_failed`, `no_active_window`,
`screenshot_failed`, `internal`. A `hello` refused with
`uiautomation_busy` or `uiautomation_failed` also carries `protocol`,
`build`, `pid` and `sdk_int`, and the helper exits right after that reply.
