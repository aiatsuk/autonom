---
name: android-emulator-browser
description: "Mirror and control an Android target or iOS Simulator in a browser through the authenticated Autonom Mobile Canvas: live scrcpy H.264 decoded with WebCodecs and real-time touch, keys, text and clipboard on Android, with screenrecord and screenshot fallbacks."
---

# Mobile Canvas browser

Stream one explicit target into a localhost browser for visual proof and
control. Install and launch the app first. HTTP input (`/tap`, `/swipe`,
`/key`, `/text`) uses the same Autonom action pipeline and journal as CLI
input. On the scrcpy transport the Canvas writes streamed input to the device
itself, because a process hop per pointer move is too slow, and then journals
one record per completed action through the same bridge. The browser is not a
second runner.

## Launch

```bash
autonom canvas serve --platform android --serial <adb-serial> --transport auto

autonom canvas serve --platform android --serial <adb-serial> --transport scrcpy --max-size 1024

autonom canvas serve --platform ios --target <simulator-udid> --transport screencap
```

Open the printed fragment-token URL in the agent browser panel. The page
exchanges it for an HttpOnly cookie and removes the fragment; a reload keeps
the session through the cookie. The startup lines name the chosen transport
and, for `auto`, why a faster one was skipped. Leave the process running and
confirm the picture moves (the `fps` in the caption under the device on
scrcpy, the Stream `Frames` count otherwise) before calling the setup
successful.

Stop the Canvas with Ctrl+C in its terminal. For a background run, send
SIGTERM to the `autonom canvas serve` process; a job started in the
background from a script ignores SIGINT. There is no separate stop command.
Stopping ends the scrcpy device server, removes its `adb forward`, and puts
back the display size and density if the Canvas changed them (see Display
size).

## Transport modes

| Mode | Android | iOS Simulator |
| --- | --- | --- |
| `auto` | scrcpy when a scrcpy-server 4.1 is found, else `screenrecord` + ffmpeg, else `screencap`; `/status` `fallback_reason` names what was skipped and why | screenshots |
| `scrcpy` | the device's own H.264 from scrcpy-server 4.1, decoded in the page, with real-time input; refuses to start (`canvas.scrcpy`) without a usable server | refused |
| `screenrecord` | device H.264 → ffmpeg → MJPEG; fails loudly when ffmpeg or device H.264 output is missing | refused |
| `screencap` | multipart screenshots, at most 10 fps, no ffmpeg | `simctl` screenshots |

Device H.264 support is probed by running `screenrecord
--output-format=h264` for a second, because Android 16 (API 36) no longer
lists that hidden option in `screenrecord --help`. Android also exposes
authenticated Annex-B H.264 at `/stream.h264` when that probe succeeds. iOS
uses public `simctl` screenshots with idb input; pixel-to-point mapping is
handled by the Canvas status channel. All multipart pages of one Canvas share
one capture loop.

`--fps` caps the multipart stream (default 15) and, only when given, scrcpy
(otherwise up to 60; scrcpy sends frames only when the screen changes).
`--max-size` (default 1280) is the longer side of the scrcpy video and the
width of the MJPEG stream. `--bit-rate` defaults to 8 Mbit/s.

## scrcpy-server

The Canvas speaks the scrcpy 4.1 protocol only and never downloads the
server. It uses the first source that is set and reports a broken one instead
of trying the next:

1. `--scrcpy-server PATH`, with `--scrcpy-version 4.1` when the file name does
   not say `scrcpy-server-v4.1`;
2. `AUTONOM_SCRCPY_SERVER`;
3. `SCRCPY_SERVER_PATH`;
4. `scrcpy` on `PATH`: the server installed beside it
   (`<prefix>/share/scrcpy/scrcpy-server`, found through Homebrew's symlink),
   at the version `scrcpy --version` prints.

A file named by a variable takes its version from its name only
(`scrcpy-server-v4.1`, optionally `.jar`); `4.1-rc1` is not 4.1.
`autonom doctor` shows the same resolution under `optional_tools.scrcpy`
(`source`, `server_path`, `version`, `ready`, `install_hint`) and never fails
`--strict` over it. Install with `brew install scrcpy`, `sudo apt-get install
scrcpy` (distribution packages may predate 4.1), or `scripts/bootstrap.sh
--install --with-scrcpy`. Another scrcpy release turns the transport off:
`auto` falls back and says why.

## The page

A slim toolbar holds the Autonom wordmark, the target with a live dot and the
transport, the Size menu (Android only, see Display size) with the current
size and density beside it, the control status, and the inspector toggle.
Below it the device picture sits in a frame whose aspect ratio follows the
video, with a caption under it (fps, round trip and decoder on scrcpy; the
transport otherwise) and a floating pill of device buttons: Back, Home,
Recent apps | Rotate, Volume down, Volume up, Power. The inspector beside it
has Type (text box and Send), Keys (Up, Down, Left, Right, Enter, Delete),
Device (Wake screen, Notifications, Quick settings, Collapse panels, Copy
device clipboard), Control (Take control or Release, Pause input or Resume
input) and Stream (transport, codec, frames, video and display size, clients,
Reconnect stream, and a Diagnostics panel with the full status text). Every
pill button, the arrows and Delete are icons named by their tooltip.

Light and dark follow the system setting. At 760 px and narrower the
inspector moves under the device, the pill stays pinned to the bottom above
the safe area, and every tap target is at least 44 px. Controls that cannot
act (paused, another owner) are disabled; the scrcpy-only ones (Rotate,
Volume down, Volume up, Notifications, Quick settings, Collapse panels, Copy
device clipboard) are hidden on the other transports. The iOS page has no Size
menu, pill, Keys or Device section. The element ids `video`, `screen`,
`status`, `text`, `clipboard`, `device` and `refresh` and
`window.autonomCanvas` are stable for scripts.

## The scrcpy page

- Decodes with WebCodecs (`VideoDecoder`, codec string from the stream's SPS,
  low-latency mode) and draws only the newest frame. The Diagnostics panel
  reads `transport: scrcpy (webcodecs)` with the codec, rendered/decoded/dropped
  frames, decoder queue, video size, session state, client counts and owner.
- Without `VideoDecoder`, when it rejects the codec, or after four refused
  video sockets it shows the multipart picture instead and says
  `scrcpy (multipart: <reason>)`; input still goes over the control socket.
- Rotation resizes the picture and keeps input mapped. A finger still down
  when the picture changes size is lifted, because scrcpy ignores events made
  for the old size; its later moves are refused, so put it down again. A
  device server that dies is restarted after 1 s, doubling to 10 s, and the
  fingers and keys it left down are lifted through the new one; lost sockets
  reconnect after 0.5 s, doubling to 5 s. Reconnect stream, under Stream in
  the inspector, re-opens the video socket (or the multipart stream).
- `window.autonomCanvas.stats()` returns those counters and
  `window.autonomCanvas.send(message)` sends one control message, so an agent
  driving the page can check health and send input without synthesizing
  pointer events.
- One device server per Canvas: it starts with the first WebSocket client,
  stops 15 s after the last one leaves, and stopping the Canvas ends it and
  removes its `adb forward`.

## Input surface

On the scrcpy transport (focus the screen for keys):

- Pointer down, move, up and cancel reach the device as they happen; a held
  finger stays pressed; up to 10 touches across all connections. The left
  mouse button is one finger; Ctrl- or Alt-drag is a two-finger pinch
  mirrored around the screen centre.
- Wheel and trackpad deltas are scroll events on both axes at the pointer,
  not swipes.
- Letters, digits, punctuation, Space, Enter, Tab, Backspace, Delete, Escape,
  Insert, arrows, Home/End, PageUp/PageDown, F1-F12, numpad operators and
  modifiers go down and up with Android meta state, so Shift+A types `A`.
  These keycodes stand for US key positions: a key that types another
  character than a US layout would there is sent as that character, as text.
  That covers other layouts (AZERTY, QWERTZ, Cyrillic, ...), the AltGr layer
  (Ctrl+Alt) and the macOS Option layer. A dead key sends nothing; the
  character it composes goes as text with the next key. Plain Ctrl and Meta
  shortcuts keep their keycodes. Input-method composition beyond dead keys
  (Chinese, Japanese or Korean input, for example) is not supported on the
  screen; use the text box. Other single characters, numpad digits included,
  are sent as text. CapsLock is not sent. Leaving the screen releases held
  keys.
- The text box takes Unicode: up to 300 UTF-8 bytes per entry as text, a
  longer entry as a paste. Ctrl/Cmd+V on the screen pastes the browser
  clipboard. A paste or entry must fit one 64 KiB control message after JSON
  escaping (quotes, backslashes and control characters count double or more);
  a larger one is not sent, the text box keeps it, and the page says why.
- Buttons: Back, Home, Recent apps, Rotate, Volume down, Volume up and Power
  in the pill; Up, Down, Left, Right, Enter and Delete under Keys; Wake
  screen, Notifications, Quick settings, Collapse panels and Copy device
  clipboard under Device. Copy device clipboard fetches the device clipboard
  into the text box and, when the browser allows it, the host clipboard.

On the other transports and on iOS: tap, drag (sent as a swipe on release),
wheel-as-swipe, the Back, Home, Recent apps, Up, Down, Left, Right, Enter,
Delete, Wake screen and Power buttons, and conservative ASCII typing over
HTTP. iOS refuses the Android key buttons, and its page does not show them.
Structural selection still belongs to Autonom `ui` commands.

### The device clipboard

On the scrcpy transport, text with any character outside printable ASCII
(tab and newline count as ASCII), a text-box entry over 300 bytes, and every
paste go through the device clipboard: the Canvas replaces the device
clipboard with that text and pastes it into the focused field. The previous
clipboard is not restored. Copies made on the device are not pushed to the
browser; use Copy device clipboard under Device. Clipboard text is never
logged or journaled.

The app reads the clipboard only when it handles the paste, so after each
clipboard paste the Canvas holds all further input, from every connection and
in arrival order, until the device acknowledges that paste and 200 ms more
have passed. Without an acknowledgement it goes on after 1 s and says so in
`/status` `last_error`. A burst of pastes and text therefore lands in the
order it was sent, at up to about four pastes a second. The 200 ms wait is a
heuristic: an app that reads the clipboard later than that after the paste
key can still lose text in a fast burst of Unicode text interleaved with
other input.

A clipboard paste only arrives if the focused app handles the Android paste
key (`KEYCODE_PASTE`, Android 7 and later). In an app that ignores it, such
text does not arrive at all, and the journal still records it: a `text` or
`paste` record means the text was sent, not that the app accepted it. Check
the field (`autonom ui find` or a screenshot) when it matters, or type ASCII,
which is injected as key events.

### Handoff

`pause`, `resume`, `takeover` and `release` govern WebSocket and HTTP input
alike and are checked per message. A refused message gets an error naming the
owner (or the pause) and never reaches the device. A pause, a takeover by
another origin, a disconnect or a device-server restart lifts every pointer
and key that connection held, and every client sees the new state. Page
connections are `human`; a token client may pass
`?origin=agent|replay|system`.

### Driving it from a script

```text
ws://127.0.0.1:<port>/ws/control?token=<token>&origin=agent   JSON text, at most 64 KiB

{"t":"touch","a":"down|move|up|cancel","id":<int>,"x":0..1,"y":0..1,"p":0..1}
{"t":"scroll","x":0..1,"y":0..1,"dx":-16..16,"dy":-16..16}
{"t":"key","a":"down|up","code":<Android keycode>,"meta":<int>,"repeat":<int>}
{"t":"text","text":"<at most 300 UTF-8 bytes>","sensitive":true|false}
{"t":"paste","text":"..."}            {"t":"clipboard-get"}
{"t":"system","op":"back|home|app-switch|power|volume-up|volume-down|wake|notifications|quick-settings|collapse|rotate|keyframe"}
{"t":"control","mode":"pause|resume|takeover|release"}      {"t":"ping","ts":<number>}
{"t":"display","preset":"small|pixel-11|pixel-fold|tablet|default"}
```

Coordinates are normalized to the current video. Replies are `state`,
`error` (with `for`), `clipboard`, `display` and `pong` messages. An invalid
message gets an error and changes nothing.

## Display size

On Android the Canvas switches the target's screen size and density with
`wm size` and `wm density`, on its own serial only (`adb -s <serial>`). The
emulator, the Canvas, its device server and open pages keep running.

| Preset | Label | Size | Density |
| --- | --- | --- | --- |
| `small` | Small phone | 720x1280 | 320 |
| `pixel-11` | Pixel 11 | 1080x2424 | 420 |
| `pixel-fold` | Pixel Fold (open) | 2208x1840 | 420 |
| `tablet` | Tablet | 2560x1600 | 320 |
| `default` | Device default | the target's own | the target's own |

A preset runs `wm size <W>x<H>` then `wm density <D>`; `default` runs
`wm size reset` and `wm density reset`. Both values are then read back and
reported as they are in effect.

- **Size menu.** The toolbar's Size button opens a list of the five presets
  with their sizes, the one in effect checked; on wider windows the current
  size and density show beside it. It opens with a click, Enter or the arrow keys; arrows,
  Home and End move, Enter or a click chooses, and Escape, Tab or a click
  elsewhere closes it unchanged. It is disabled while this page cannot send
  input (paused, or another origin holds control) and while a change runs,
  and it is not shown on iOS. A choice goes over `/ws/control` on scrcpy and
  `POST /display` otherwise, and every open page then shows it. "Other size"
  means the device carries an override this Canvas did not make.
- **Agent API.** `{"t":"display","preset":"<id>"}` on `/ws/control` (scrcpy
  only), or `POST /display` with `{"preset":"<id>"}` on every Android
  transport, under the same token or cookie plus `X-Autonom-CSRF`, Host,
  Origin and handoff rules as other input (`X-Autonom-Origin: agent` for an
  agent over HTTP). The answer comes once the change has read back:
  `{"t":"display","ok":true,"display":{"preset","width","height","density"}}`
  on the socket, `200 {"ok":true,"display":{...}}` over HTTP.
- **Refusals** run no `wm` command: an unknown preset (400, naming the valid
  ids), iOS (400, `Display presets are Android-only`), paused or owned by
  another origin (409, as for other input), a stopping Canvas (503). On the
  socket a refusal is an `error` with `for: "display"`.
- **Order.** Changes run one at a time, in arrival order with HTTP input. At
  most one waits: a newer request takes its place at the end of the queue,
  and the replaced one is answered `superseded` (`superseded: true`, HTTP
  409) without running anything. Before a change the Canvas lifts every held
  finger and key, as a takeover does.
- **Failures.** A `wm` command that fails, or a read-back that differs from
  the preset, answers 502 (an `error` on the socket) naming the failed
  command and the size and density in effect. The Canvas keeps running, and a
  later change or the restore at stop still works.
- **Picture.** scrcpy-server follows with a new video size and key frame,
  without a restart or reconnect; as for a rotation, a finger still down is
  lifted, and a touch sent for the old size is dropped rather than landing in
  the wrong place. screenrecord restarts its shared capture; screencap needs
  nothing. HTTP taps use the size read back.
- **Status.** On Android `/status` `display` carries `width`, `height`,
  `density`, `preset` and `presets` (`id`, `label`, `width`, `height`,
  `density`; `default` has no numbers), from a `wm` reading at most 2 s old.
  `preset` is a preset id only after its commands succeeded and the read-back
  matches it, `default` with no override, and `null` for an override this
  Canvas did not make. `state` messages carry `presets` and, from a reading
  at most 2 s old, `preset` and `density`. Each change prints
  `Display: <preset> <W>x<H> @ <density>` and writes one `ui display`
  journal record.
- **Restore at stop.** Before its first change the Canvas records the
  target's size and density overrides, or their absence. On SIGINT, SIGTERM
  or SIGHUP it puts exactly those back (re-applying an earlier override, or
  resetting) within the 3 s shutdown budget, before it exits. It waits only
  for a change already running; queued input and queued changes are dropped.
  It prints `Display restored: <W>x<H> @ <density>` (or `Display restore
  failed: ...` with what is in effect) and journals one `ui display` record
  with origin `system` and preset `restore`. A Canvas that changed nothing
  runs no `wm` command at stop.

While a preset is in effect, Autonom `ui` commands and screenshots see the
new size too.

Limitations:

- A Canvas killed with SIGKILL, or one that crashes, cannot restore: the
  device keeps the preset. Check with `adb -s <serial> shell wm size` and
  `wm density` (an `Override` line means a change is still in effect) and
  put it back by hand:

  ```bash
  adb -s <serial> shell wm size reset
  adb -s <serial> shell wm density reset
  ```

  For a device that had its own override before, set those values again
  instead (`wm size <W>x<H>`, `wm density <D>`).
- Two Canvases on one device: the second records the first one's preset as
  its original and puts that back when it stops. Stop them in the reverse
  order of their first display change, or reset by hand as above.

## Journal

The Canvas journals into the current Autonom session only when that session
is on the same target as the Canvas. With no current session, or one on
another device, every action still runs and nothing is journaled; start
`autonom session start --serial <adb-serial>` on the Canvas target first to
keep a record. This holds for HTTP input and the scrcpy path alike.

Every completed action on the scrcpy path becomes exactly one journal entry
(`ui gesture|scroll|key|text|paste|system|control`) with its origin and
`transport: scrcpy`:

| Kind | One record per | Recorded |
| --- | --- | --- |
| gesture | first finger down to last finger up | `pointers`, `moves`, `duration_ms`, `start`/`end` in device pixels |
| scroll | wheel burst, ended by 400 ms of stillness or other input | `events`, `dx`, `dy` |
| key | key press, at key up (repeats included) | `key` (Android keycode) |
| text | text entry | `text_len`, and `text` unless `sensitive` |
| paste | paste | `text_len` only |
| system | button | `op` |
| control | handoff change | `mode`, `owner` |

A display change writes one `ui display` record on every Android transport,
with `preset`, `width`, `height`, `density` (as read back), its origin and
the Canvas `transport`; the restore at stop writes one with origin `system`
and preset `restore`.

Input the Canvas lifts on pause, takeover, disconnect or restart is
journaled like input its client released. Page text and key presses are
journaled; type secrets with `autonom ui type --sensitive` instead.
`/status` `scrcpy.journal_pending` counts records waiting for the bridge.
The Canvas stops reading a connection's input while 64 of its records, or
256 in all, are unanswered, so ordinary input waits instead of being lost; a
record produced while 256 wait (a pointer lifted by a takeover during a
flood) is dropped and counted in `scrcpy.journal_dropped`.

## Security

- Binds `127.0.0.1` with a random token by default. Do not expose publicly.
  Anyone with a forwarded port and the bootstrap token can drive the device.
- Every request and WebSocket upgrade must carry `Host: 127.0.0.1:<port>` or
  `localhost:<port>` (DNS-rebinding guard), so a forward must keep the port.
- A WebSocket upgrade needs the session cookie plus that session's `csrf`
  query value and the Canvas `Origin`, or the token (`?token=` or
  `Authorization: Bearer`). A foreign `Origin` is refused whatever it carries.
- The page cannot be framed (`frame-ancestors 'none'`, `X-Frame-Options:
  DENY`, framed loads refused), and a reload gets its CSRF value back only
  from the Canvas's own origin.
- Messages are validated strictly (finite numbers, ranges, enums, a key
  allowlist, sizes) before anything reaches the device.
- Memory stays bounded under load, but input is not rate-limited: a client
  that already holds the token or cookie can flood the control socket and
  make input slow, delay journaling, or get closed (1013). That is accepted,
  since such a client can drive the device anyway.
- `--no-auth` is for isolated local debugging only.

## Performance boundary

Good for iteration; not a measurement tool. Emulators encode H.264 in
software, so the frame rate depends on the emulator's GPU mode and the
encoded size. Start the emulator with `-gpu host`, and lower `--max-size` for
more frames. In one measurement on an API 36 emulator (1080x2424), a
`-no-window` emulator in its default GPU mode rendered about 13 fps;
`-gpu host` gave about 38 fps at `--max-size 1280` with about 100 ms from
touch to picture, and the device produced about 44 frames/s at 1024 and 56 at
720. Use scrcpy itself for high-fidelity manual review and Macrobenchmark /
Perfetto / Flutter profile mode for claims.

On an emulator with auto-rotate on, the sensor turns the screen back to
portrait right after Rotate; turn auto-rotate off on the device for a
rotation that sticks.

## Evidence to record

Side-panel screenshot, platform and target id, the Diagnostics panel's
transport and decoder lines, `fallback_reason`, `scrcpy.version`/`scrcpy.source`
and the display preset, size and density from `/status`, package/activity,
variant, control owner, and the exact flow replayed.
