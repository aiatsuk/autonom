---
name: android-emulator-browser
description: Mirror and lightly control an Android target or iOS Simulator in the visible Codex browser through the authenticated Autonom Mobile Canvas.
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
confirm the picture moves (the status panel's `fps` on scrcpy, `frames`
otherwise) before calling the setup successful.

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

## The scrcpy page

- Decodes with WebCodecs (`VideoDecoder`, codec string from the stream's SPS,
  low-latency mode) and draws only the newest frame. The status panel reads
  `transport: scrcpy (webcodecs)` with the codec, rendered/decoded/dropped
  frames, decoder queue, video size, session state, client counts and owner.
- Without `VideoDecoder`, when it rejects the codec, or after four refused
  video sockets it shows the multipart picture instead and says
  `scrcpy (multipart: <reason>)`; input still goes over the control socket.
- Rotation resizes the picture and keeps input mapped. A finger still down
  when the picture changes size is lifted, because scrcpy ignores events made
  for the old size; its later moves are refused, so put it down again. A
  device server that dies is restarted after 1 s, doubling to 10 s, and the
  fingers and keys it left down are lifted through the new one; lost sockets
  reconnect after 0.5 s, doubling to 5 s. Reconnect re-opens the video socket
  (or the multipart stream).
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
- Buttons: Back, Home, Apps, arrows, Enter, Delete, Wake, Power, Rotate,
  Volume down/up, Alerts (notifications), Quick (quick settings), Collapse,
  and Clipboard, which fetches the device clipboard into the text box and,
  when the browser allows it, the host clipboard.

On the other transports and on iOS: tap, drag (sent as a swipe on release),
wheel-as-swipe, Back/Home/Apps/Enter/Delete/D-pad/Wake/Power, and
conservative ASCII typing over HTTP. iOS refuses the Android key buttons.
Structural selection still belongs to Autonom `ui` commands.

### The device clipboard

On the scrcpy transport, text with any character outside printable ASCII
(tab and newline count as ASCII), a text-box entry over 300 bytes, and every
paste go through the device clipboard: the Canvas replaces the device
clipboard with that text and pastes it into the focused field. The previous
clipboard is not restored. Copies made on the device are not pushed to the
browser; use Clipboard. Clipboard text is never logged or journaled.

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
```

Coordinates are normalized to the current video. Replies are `state`,
`error` (with `for`), `clipboard` and `pong` messages. An invalid message
gets an error and changes nothing.

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

Side-panel screenshot, platform and target id, the status panel's transport
and decoder lines, `fallback_reason` and `scrcpy.version`/`scrcpy.source`
from `/status`, package/activity, variant, control owner, and the exact flow
replayed.
