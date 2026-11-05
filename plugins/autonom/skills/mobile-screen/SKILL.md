---
name: mobile-screen
description: Understand and control what is on an Android or iOS screen for AI agents using compact accessibility/UI trees, semantic find/tap, gestures, screenshots, and key/text input via the Autonom CLI.
model: inherit
---

# Mobile Screen (Android + iOS Simulator)

## Purpose

Answer **what is on screen** and **act on it** without relying on pixel-only guessing.

1. Prefer a **compact UI tree** (Autonom's on-device helper or UI Automator on Android,
   accessibility tree via idb or XCUITest on iOS).
2. Confirm with a **screenshot** when visual layout matters or the tree is ambiguous.
3. Act with **semantic selectors** first; coordinates only as fallback.

The compact node schema is identical on both platforms, so the same reasoning works
either way. What differs is **which field carries the visible label** — see below.

## CLI

```bash
# Agent session: `ui tree` prints the outline, and every tap/type/swipe/key returns the new screen
python3 <autonom-root>/scripts/autonom.py session start --profile agent --app-id <app-id>

# Compact tree (meaningful nodes by default)
python3 <autonom-root>/scripts/autonom.py ui tree
python3 <autonom-root>/scripts/autonom.py --platform ios --target <UDID> ui tree
python3 <autonom-root>/scripts/autonom.py ui tree --format outline --interactable   # one indented line per actionable node

# Android emulator Flutter semantics recovery (with an active app session)
python3 <autonom-root>/scripts/autonom.py ui accessibility status
python3 <autonom-root>/scripts/autonom.py ui accessibility enable   # explicit recovery
python3 <autonom-root>/scripts/autonom.py ui accessibility reset    # restore saved settings
python3 <autonom-root>/scripts/autonom.py ui tree --no-accessibility-recovery  # inspection only

# Wait until the screen stops changing (animations, spinners, loading lists)
python3 <autonom-root>/scripts/autonom.py ui wait --settled --timeout-ms 5000       # settled, snapshots, changes, elapsed_ms; exit 1 if never settled

# Find
python3 <autonom-root>/scripts/autonom.py ui find --text "Login" --mode contains     # Android
python3 <autonom-root>/scripts/autonom.py ui find --desc "Log In" --mode exact       # iOS
python3 <autonom-root>/scripts/autonom.py ui find --resource-id com.apple.settings.general

# Act
python3 <autonom-root>/scripts/autonom.py ui tap --desc "Continue"
python3 <autonom-root>/scripts/autonom.py ui tap --x 540 --y 1600
python3 <autonom-root>/scripts/autonom.py ui type "user@example.com"
python3 <autonom-root>/scripts/autonom.py ui fill "user@example.com" --resource-id com.example:id/email   # Android: replace the field's text
python3 <autonom-root>/scripts/autonom.py ui swipe --from 200,600 --to 200,200
python3 <autonom-root>/scripts/autonom.py ui key KEYCODE_BACK      # Android
python3 <autonom-root>/scripts/autonom.py ui key HOME              # iOS
python3 <autonom-root>/scripts/autonom.py ui tap --desc "Continue" --observe        # outside an agent session: also return the new screen
python3 <autonom-root>/scripts/autonom.py ui swipe --from 200,600 --to 200,200 --no-observe   # agent session: return right after the input

# A known path in one call: Flow v1 steps on stdin, stops at the first failing step
echo '[{"tapOn":{"visibleText":"General"}},{"assertVisible":{"visibleText":"About"}}]' \
  | python3 <autonom-root>/scripts/autonom.py ui do

# Android: the on-device helper that serves trees and input
python3 <autonom-root>/scripts/autonom.py ui helper status   # running, build, pid, port, idle_ms
python3 <autonom-root>/scripts/autonom.py ui helper stop     # free the device for uiautomator2, Appium or a raw dump

# Evidence
python3 <autonom-root>/scripts/autonom.py screenshot --out /tmp/screen.png
python3 <autonom-root>/scripts/autonom.py screenshot --agent   # Android: small JPEG, scale and a tap-coordinate note
python3 <autonom-root>/scripts/autonom.py record start --name login
python3 <autonom-root>/scripts/autonom.py record stop

# Offline parse of a dump (CI / fixtures) — the platform is detected from the file
python3 <autonom-root>/scripts/autonom.py ui tree --dump tests/fixtures/ui_dump.xml
python3 <autonom-root>/scripts/autonom.py ui tree --dump tests/fixtures/idb_describe_all_sample.json
```

Every reply is one line of JSON.

## Agent profile: the new screen in every reply

Start the session you drive with `session start --profile agent`. In that
session:

- `ui tree` prints the compact outline, one indented line per node:
  `ref role "label" id=... @x,y wxh [state]`. `--format json` still gives `nodes`.
- `ui tap`, `ui type`, `ui fill`, `ui swipe` and `ui key` wait for the screen
  to settle after the input (at most `--observe-timeout-ms`, default 3000) and
  add the new screen to their own reply:
  - `screen.outline` — the screen now, as `ui tree` prints it (`count`,
    `truncated`);
  - `changes` — `added`, `removed` and `changed` counts and up to 20 outline
    lines marked `+` (new), `-` (gone) or `~` (changed). It is `null`, with
    `changes_baseline: none`, when there was nothing to compare with; read
    `screen.outline` itself then;
  - `foreground` — `app` (Android package, iOS app label) and, when known,
    `screen` (Android activity, iOS first heading): a tap that left the app
    shows here;
  - on Android, `screen.signals` (a keyboard or a dialog, below); through
    the on-device helper the reply also says `settled` and `settle_ms`.

**Read the next step from that reply.** Do not follow every action with
`ui tree` or `ui wait --settled`: the reply already waited and read the screen
once. Read the tree yourself only when the reply says so:

- `observe_error` — the input **was** sent; only the read after it failed.
  Run `ui tree`. Never send the input again.
- `input_confirmed: false` with an `input_unconfirmed` warning (iOS daemon
  route) — the input **was** sent once, but the app did not report the touch
  within 500 ms (1.5 s for the first touch on a new companion or after an app launch). Read `screen.outline`: if it shows the expected screen, carry
  on; if not, the tap probably did not land. Never repeat it blindly: read the
  tree, then decide. `ios_input_blocked` instead means input is not reaching
  the Simulator at all (its HID flag is stuck): ask for the Simulator to be
  rebooted; Autonom never reboots it.
- a `screen_not_settled` warning — the screen may still have been changing
  when the wait ended. If the returned `screen` already shows what you
  expected (the element or page you tapped toward), carry on with the next
  step. Only when it does not, re-read: `ui wait --settled`, then `ui tree`,
  or pass a larger `--observe-timeout-ms`.
  - On Android through the on-device helper (the default) the input and the
    settled screen come back in one round trip: the helper waits for the
    input's first accessibility event, then for 500 ms without events, at
    most 2 s. The warning comes only with `settled: false`, and its text
    names what kept sending events. A screen that never goes quiet (a
    running clock, a video, an animation) is `settled: false` on every read:
    when its `screen` shows what you expected, carry on, because waiting
    longer does not help.
  - On the uiautomator fallback (`--ui-backend uiautomator`, or a reply that
    carries `fallback_reason`) one read takes about 2 s, so the warning is
    common there while the screen is already right.

`--no-observe` returns right after the input (a quick run of scrolls or key
presses); `--observe` gives the same reply in a session without the agent
profile.

## `ui do`: a known path in one call

When the steps are already known — a path walked before, a runbook from
`mobile-memory`, the way back to a start screen — send them in one call
instead of one action per turn. `ui do` reads Flow v1 steps from stdin, as a
JSON array (above) or a YAML step list (the body of a flow file, without its
header), and runs them through the flow engine: exact selectors, assertions
that wait until the screen shows what they expect, and every input sent once.

- Select with `visibleText` (the label, wherever the platform keeps it),
  `id`, `text` or `description`. `text` alone misses iOS labels, as `--text` does.
- Put an `assertVisible` after a step whose screen matters, so a wrong
  screen stops the run instead of the next tap landing on it.
- It stops at the first failing step. The reply has `ok`, `steps` (each with
  its `status`), `failure` (`step`, `code`, `message`) and the final `screen`,
  `changes` and `foreground`, as after an observed tap. Exit 0 when every step
  passed, 1 when one failed.
- A mistake in the steps (not JSON or YAML, an unknown command, `back` on
  iOS) is `invalid_value` naming the step, before any step runs.
- iOS has no `back` step: tap the navigation bar's back control (in Settings
  `{"tapOn":{"id":"BackButton"}}`).
- Explore an unknown screen step by step with observed actions; use `ui do`
  only for what you already know. A path you will run again belongs in a flow
  file (`mobile-flow`).

## Android: the on-device helper

On Android, Autonom reads the screen and sends input through its own small
helper program on the device (`--ui-backend auto`, the default). The first
Android call builds it from the local JDK and Android SDK (seconds, then
cached); a read then takes tens of milliseconds instead of the ~2 s of
`uiautomator dump`.

- Replies say what served them: `ui_backend` (`helper` or `uiautomator`)
  and `input_backend` (`helper` or `adb`); reads through the helper add
  `settled` and `settle_ms`.
- **Fallback.** Without a JDK or SDK, or on a device where the helper cannot
  run, the call still works through uiautomator and adb input and says why:
  `fallback_reason`, and `fallback_hint` naming what to install or set. Tell
  the user once when speed or non-ASCII typing matters, then carry on.
- **Opt-out.** `--ui-backend uiautomator` (or
  `AUTONOM_ANDROID_UI_BACKEND=uiautomator`, or a session started with it)
  uses only uiautomator and adb input, with the replies Autonom gave before
  the helper (no `ui_backend` keys).
  `--ui-backend helper` insists on the helper and fails with
  `android_helper_unavailable` instead of falling back.
- A screen that never goes idle (an animation, a video, a timer) is still
  read through the helper, marked `settled: false`, instead of `ui_not_idle`.
- **Free the device for other tools.** While it runs, the helper holds the
  device's UiAutomation, which uiautomator2, Appium, instrumentation tests
  and a raw `uiautomator dump` need too. It stops by itself after 2 minutes
  without a call; `ui helper stop` frees the device at once (`ui helper
  status` says whether one runs), and `session stop` stops it too.
- `ui_action_uncertain` after a helper input: the input was written and no
  answer came, so it may have happened. Read the screen; never send it again.
- `backend_failed` with `helper_busy` (`android_helper_unavailable` under
  `--ui-backend helper`): another Autonom process was using the device's
  helper for 20 s, and nothing was sent. Retry once it is done.

## Typing and filling fields

- `ui type TEXT` types at the cursor of the focused field. On Android through
  the helper any text works (accents, Cyrillic, CJK, emoji); the reply adds
  `text_after` (what the field shows) and `complete`. `complete: false` with
  a `text_incomplete` warning means part of the text did not land: typing
  stopped there and nothing was sent twice. Read the field, then type the
  rest.
- `ui fill TEXT` (Android) replaces the whole text of a field: name it with
  the selector flags of `ui tap`, or leave them out to fill the focused one.
  Check `text_matches` in the reply. Prefer it to tapping, deleting and
  typing. On iOS it is `unsupported_on_platform`: use `ui type`.
- `text_not_typable`, nothing typed: on the uiautomator fallback only
  printable ASCII can be typed; through the helper, the text needs set-text
  in a field whose text cannot be read (a password field).
- `fill_target_mismatch`, nothing typed: the tap did not give the field you
  named the input focus. Read the screen and select it again.
- An Android key (`ui key`, a Flow `pressKey`, a Canvas key) is a `KEYCODE_*`
  name, a bare upper-case name such as `BACK` or `HOME` (sent as
  `KEYCODE_BACK`, `KEYCODE_HOME`) or a key number of up to 4 digits.
  Anything else (lower case, spaces, shell text, a longer number) is
  `invalid_value` and nothing is sent.

## Keyboard and dialog signals (Android)

Android reads (`ui tree`, an action's `screen`, `ui do`'s final screen) carry
`signals`, except under the explicit `--ui-backend uiautomator` choice. They
only report: Autonom never taps or dismisses anything for you.

- `signals.keyboard` — `visible`, `package`, `bounds`: the on-screen keyboard
  covers that part of the screen. A control under it cannot be tapped: press
  `KEYCODE_BACK` to hide the keyboard, or swipe the control into view.
  `null` means unknown (the uiautomator fallback).
- `signals.dialog` — `kind` (`permission`, `alert` or `window`), `package`,
  `title` and `buttons` (`label`, `resource_id`): a dialog is over the app.
  `window` is a dialog in its own window (helper reads only): a popup over
  the app, or a modal dialog such as a Compose one, seen from the active
  window leaving the screen partly uncovered. Decide which button the task
  needs and tap it yourself. `null` when none.

## Screenshots for an agent (Android)

`screenshot --agent` (the default for `screenshot` in an agent-profile
session on Android) makes a small JPEG, long edge at most 1000 px
(`--max-edge N` to change it), instead of the full-size PNG. Its pixels are
**not** screen pixels. The reply says how to convert: `scale` (screen pixels
per image pixel) and `coordinates`, for example
`image pixel (x, y) → ui tap --x round(x*2.4) --y round(y*2.4)`. Prefer a
selector tap; tap by coordinates only when the tree has no node for what you
see, and convert with that note first. Without the helper the reply is the
full-size PNG with `scale: 1` and `fallback_reason`; `--no-agent` asks for the
PNG.

## Compact node schema (identical on both platforms)

```json
{
  "ref": "n5",
  "role": "button",
  "text": null,
  "desc": "General",
  "resource_id": "com.apple.settings.general",
  "bounds": [16, 380, 386, 432],
  "clickable": true,
  "long_clickable": false,
  "checkable": false,
  "enabled": true
}
```

Text fields also carry `editable: true`, and `password: true` when the field
hides its text. In the outline every switch and checkbox says `checked` or
`unchecked`, so an off switch is visible as off; text fields say `editable`
(and `password`).

## Where the visible label lives

**This is the one asymmetry that matters.**

| Platform | Visible label lands in | Select with |
| --- | --- | --- |
| Android | `text` (and `desc` for content-description) | `--text`, `--desc`, `--resource-id` |
| iOS | **`desc`** (from `AXLabel`); `text` comes from `AXValue` and is often `null` | **`--desc`**, `--resource-id` |

On iOS a control labelled "General" has `desc: "General"` and `text: null`, so
`ui find --text "General"` returns **zero matches**. Read the tree first and select
by `--desc`, or by `--resource-id` when the app sets accessibility identifiers.
Flutter on Android puts its labels in `desc` (content-description) too. On both
platforms, when a `--text` miss would have matched a `desc`, the response says so
with a `label_is_in_desc` warning whose hint is the `--desc` retry. A `ui find` or
`ui tap` with no selector at all is `selector_required`. Whitespace is normalised
(iOS joins a merged Flutter label with a newline, Android with a space), except
in `--mode regex`.

## What is on screen, and which match is picked

iOS lists Flutter's scroll cache — nodes laid out but not on screen — at a 0x0
frame. Such a node stays in `ui tree` (it is counted) marked `visible: false`,
and so does any node entirely outside the app's frame, live or from `--dump`.

- `ui tap` and `ui find` resolve **on-screen matches first**: duplicates and
  `--index` are counted among them, exactly as a flow counts, so a selector
  that is unique on Android is not ambiguous on iOS because of an off-screen
  copy. Only when no match is on screen is every match counted.
- `ui tap` on a node that was never laid out on screen refuses with
  `element_offscreen` (with `ref`, `bounds`, `match_count`) and taps nothing.
  Swipe it into view, then select it again.
- `ui find --all` lists on-screen matches first, then off-screen ones. Every
  match `ui find` reports, with or without `--all`, carries `index` — the
  `--index` that selects it (`null` for an off-screen match no index reaches
  while another match is on screen). Position *i* of the `--all` list is what
  `--index i` taps.
- `ui wait --settled` on Android through the helper waits inside one read
  for `--quiet-ms` without accessibility events (`method: helper`,
  `snapshots: 1`, `busy` naming what kept changing when it did not settle).
  On the iOS idb route it asks idb whether the app is quiet and then reads
  once (`method: idb-quiet`, `snapshots: 1`; `method: quiet-stream` when the
  iOS device daemon answered). Elsewhere it polls
  (`method: poll`) and says the tree was "still changing" only when its last
  snapshot was a change. If it changed and then held still for less than
  `--quiet-ms`, it says that (`stable_ms`); if only one dump fit in
  `--timeout-ms` (a uiautomator dump can take ~2 s), it says settling could
  not be confirmed and how far to raise it.
- `ui_not_idle` (Android, uiautomator reads only) means the screen never
  stops changing — an animation, a video, a timer, a spinner — so UI
  Automator cannot read it. Waiting does not help: take `autonom screenshot`
  and act on what it shows, or leave that screen. Through the helper the same
  screen is read and marked `settled: false`.

## Which tool touched the screen (`backend`)

Every input payload (`ui tap`, `ui swipe`, `ui type`, `ui fill`, `ui key`)
names its `backend`: `helper` or `adb` on Android (see the helper section
above); `idb` or `axe` on iOS. The iOS tree always comes
from idb. On Xcode 27 an idb companion built before the fix cannot load
SimulatorKit, so the tree works while input fails with
`ios_hid_framework_missing`; with AXe installed
(`brew install cameroncooke/axe/axe`) the default `--ios-hid auto` sends
input through AXe instead. `--ios-hid idb|axe` (or `AUTONOM_IOS_HID`) pins a
route, and `--axe PATH` (or `AUTONOM_AXE`) names the binary; both go before
the verb. `autonom doctor` reports `ios_tree` and `ios_hid` separately with
the fix for each. On Xcode 27 with AXe carrying input, `ios_hid.ready` stays
`false` (idb's own HID) while `ios_hid.axe_ready` and `ios_ui.ready` are
`true`: read `ios_ui` and `axe_ready` — input works, do not give up on it.

## The iOS device daemon (`transport`)

On the iOS idb route one small Node process per Simulator, the **device
daemon**, keeps the connection to the Simulator's one `idb_companion` open.
It holds no input call open between inputs (each ends 250 ms after its last
event), so `ui type`, idb buttons and the Canvas are not blocked by it.
It starts on the first `ui` call (Node 22+ needed), serves tree reads, `ui
wait --settled`, taps, swipes, buttons and keys, and stops by itself after
**5 minutes** with no request and no Canvas attached. `ui type`, crash
reports, file pull, screenshots, a remote companion and the XCUITest route
still use the `idb` process. Nothing to start or stop by hand: `session stop`
leaves the daemon's companion to it (it is not reported as
`companion_left_running`), and the Canvas streams on the same companion.

Each iOS reply on the idb route says how it was served:

- `transport: daemon` — the daemon answered; `transport: subprocess` — the
  `idb` process did. With `subprocess`, `transport_reason` says why the
  daemon was not used: `daemon_disabled` (`AUTONOM_IOS_DAEMON=off`),
  `node_missing`, `daemon_start_failed`, `companion_too_old` (the companion
  is older than 1.6.4) or `daemon_unreachable`. The call still worked; only
  the speed differs.
- `ax_backend` — which accessibility read made the tree: `ax` (native
  screens), or `axbridge` for web content (Safari, a web view, or a native
  read that covered too little of the screen). Web page text shows up only
  in `axbridge` reads. `backend` still names the input route (`idb`, `axe`).
- On an observed action: `input_confirmed` (the app reported the touch) and
  `method: quiet-stream`; `ui wait --settled` also answers
  `method: quiet-stream`.

Codes to know:

| Code | Meaning | What to do |
| --- | --- | --- |
| `input_unconfirmed` (warning) | the input was sent once; the app did not report the touch in 500 ms (1.5 s for the first touch on a new companion or after an app launch) | read the returned screen; never resend blindly |
| `input_unconfirmed` (error) | the input was sent once, but `idb_companion` never confirmed it: another client held the Simulator's input past 15 s, or the companion was lost | run `ui tree` before you repeat it; it was not sent again |
| `ios_input_blocked` (warning) | the Simulator's HID flag is set and no Device Hub runs: input is lost | have the Simulator rebooted |
| `ios_display_not_lit` (error) | the display is not lit yet (a foldable, a display change) | wait and read again, or use a device type with one fixed display |
| `companion_too_old` (`transport_reason`) | `idb_companion` older than 1.6.4 | works through `idb`; upgrade the companion for speed |

An input whose daemon reply was lost after it was written fails
`idb_companion_unavailable`, and one the companion never confirmed fails
`input_unconfirmed`; neither is **sent again**: read the screen before doing
anything else. An input the daemon could not start within its 15 s limit is
refused unsent and goes through the `idb` process once. To rule the daemon
out while debugging, set `AUTONOM_IOS_DAEMON=off`; every call then goes
through the `idb` process.

## Gestures and keys by platform

| Verb | Android | iOS |
| --- | --- | --- |
| `ui tap`, `ui swipe`, `ui type` | yes | yes |
| `ui fill` | yes | refused with `unsupported_on_platform` (use `ui type`) |
| `ui pinch` | scrcpy 4.1 + Node.js | idb_companion HIDPinch + Node.js |
| `ui rotate`, `ui shake` | refused with `unsupported_on_platform` | refused with `unsupported_on_platform` |
| `ui key` | a `KEYCODE_*` name, a bare upper-case name (`BACK`), or a key number (up to 4 digits) | `HOME`, `LOCK`, `SIDE_BUTTON`, `SIRI`, `APPLE_PAY`, or a numeric HID code |

`ui pinch --at X,Y --scale 2` zooms in; `--scale 0.5` zooms out. The default
duration is 0.4 seconds. The center is in Android pixels or iOS points; the
command measures the screen and chooses a radius that fits. Android needs
scrcpy 4.1, resolved as for Canvas (`AUTONOM_SCRCPY_SERVER`,
`SCRCPY_SERVER_PATH`, or installed scrcpy). iOS uses idb_companion directly,
not the idb CLI; on the AXe or XCUITest route (chosen, or fallen back to)
pinch is refused with `unsupported_capability` before any device call.
Both paths require Node.js. Scale is the ratio of finger separation, not a
promise of an exact app zoom: gesture thresholds and zoom limits can change
the effect. A `ui_action_uncertain` result means input may
have happened: inspect the screen before retrying. Rotate and shake remain
unsupported; rotate the Simulator window by hand if needed.

iOS has no global Back button; tap the navigation bar's back control instead.

**Typing needs focus.** `ui type` sends keystrokes to whatever has keyboard
focus. Through the Android helper, nothing focused is the error
`no_focused_field` and nothing is typed. On iOS and on the Android
uiautomator fallback, the characters vanish while the command still exits 0:
the response carries `focused` (the node that will receive the text) with
`focus: verified` on Android or `unverified` on iOS — idb's dump has no focus
attribute, so on iOS the check degrades to "a text field is on screen" — or a
`no_focused_field` warning. Tap the field, wait for it, type, then confirm
with `ui find` on the typed text (or read `text_after`). In flows,
`inputText` refuses to type into nothing (`flow_no_focused_field`).

## Coordinate space

iOS accessibility frames and `idb ui tap` both use **points**, not pixels. Never
multiply by the display scale. A tap computed outside the target's reported screen
rectangle is refused with `coordinate_space_mismatch` rather than dispatched — on a
3x device a pixel mix-up would otherwise land silently in the wrong place and make
the agent report a defect that does not exist.

## Deterministic captures

Two screenshots of the same screen differ by the battery glyph and the
signal bars unless the status bar is pinned — and a before/after comparison
then reports a change the app never made. Pin it once per session, on either
platform, before the first capture you intend to compare. The clock stays
real, because evidence should say when it was taken; the marketing 9:41 is
one key away:

```bash
python3 <autonom-root>/scripts/autonom.py simulator status-bar pin        # full battery, full signal, no notification icons, real clock
python3 <autonom-root>/scripts/autonom.py simulator status-bar pin --value hhmm=0941   # Android: 9:41 via demo mode (clock frozen); iOS: time=9:41
python3 <autonom-root>/scripts/autonom.py simulator status-bar clear      # restore the live bar when done
```

On Android the default pin never enters SystemUI demo mode, because demo mode
freezes the clock at the moment it is entered; `hhmm=`, `wifi=`, `mobile=`, or
`mode=demo` opt into it when a fixed clock or shaped Wi-Fi bars matter more
than a ticking one. `values.mode` in the response says which path ran.
The emulator's signal cannot be read back, so a live Android pin carries a
`signal_unstable` warning and does not claim the signal as verified. The
emulator's bars and battery icon drift within a minute of any pin, so
`autonom screenshot` and flow captures re-send the live pin right before the
frame (`repinned: true` in the response) — take evidence through them, not a
raw `adb screencap`. `status-bar clear` restores the battery and signal state recorded before the
first pin.

A pin is bound to the emulator it was set on (AVD name and boot id): an
emulator that took the same serial later — another AVD, or a reboot — never
receives it. The record is dropped instead (`stale_pin_dropped: true` and a
warning), and `devices shutdown` / `devices boot` drop it outright; pin again
on the new device. On API 36 emulators SystemUI may not draw the battery
glyph at all even though `dumpsys battery` reports the pinned level, so a
missing battery icon in a capture is the emulator's, not the pin's or the
app's.

On Android, turn system animations off before comparing captures, and wait
for the screen to settle:

```bash
python3 <autonom-root>/scripts/autonom.py simulator animations pin        # window, transition, animator scales to 0
python3 <autonom-root>/scripts/autonom.py ui wait --settled
python3 <autonom-root>/scripts/autonom.py simulator animations reset      # restores what the first pin replaced
```

iOS has no host-level animation switch (`unsupported_capability`).
Every `simulator` control answers `verified: true` only after reading the
state back; otherwise `verified: false` with the reason in `verification`.

`ui type` on iOS is at the mercy of autocorrect: a non-English keyboard can
rewrite typed text mid-flow.
Pin the keyboard and locale on the **shut-down** simulator before typed text
must be exact (`reboot=true` lets the verb cycle a booted one):

```bash
python3 <autonom-root>/scripts/autonom.py simulator keyboard pin --value locale=en-US --value reboot=true
python3 <autonom-root>/scripts/autonom.py simulator keyboard show          # pinned: true|false, per key; backup: true while pinned
python3 <autonom-root>/scripts/autonom.py simulator keyboard reset         # restores the values pin replaced
```

`pin` snapshots the keys it overwrites (a region-override locale, a keyboard
setting a human chose) and `reset` puts them back, so a shared simulator
leaves a test the way it entered it. Always pair a pin with a reset.

Android has no host-level keyboard store (the settings live inside Gboard), so
`simulator keyboard` refuses there with `unsupported_capability`; turn
suggestions off in the field or the keyboard app instead.

Every `screenshot` (and `shots show`) reports `width` and `height` from the
image itself. On iOS that is **pixels** while the tree and `ui tap` speak
points; on Android a PNG is whatever the AVD's screen is, and an agent JPEG
is scaled down (convert with its `scale` and `coordinates`, above). State
the space you computed in when you report a coordinate.

## Agent workflow

1. `session start --profile agent` so the target and artifacts are explicit and
   every action returns the new screen.
2. `ui tree` once → read labels, roles, enabled and checked state from the
   outline. Note which field holds the label.
3. `ui tap` / `ui type` / `ui fill` / `ui swipe` / `ui key` for the next
   action, then read the reply's `screen`, `changes` and `foreground` — no
   `ui tree` or `ui wait --settled` in between unless the reply carries
   `observe_error`, or a `screen_not_settled` warning and a `screen` that does
   not show what you expected (common on the Android uiautomator fallback
   while the screen is already right). On Android, look at `screen.signals`
   too: a dialog or the keyboard may cover what you want next. Without the
   agent profile, add `--observe`, or run `ui wait --settled` and `ui tree`
   after each navigation.
4. A path whose steps you already know goes in one `ui do`, with an
   `assertVisible` where the screen matters.
5. `screenshot` after state changes that need visual proof — **compare before/after**;
   an exit code of 0 does not prove the screen changed. Pin the status bar first
   so the diff shows only what the app changed. In an agent session on Android
   it is the small JPEG: tap from the tree, or convert with `coordinates`.
6. Before handing the device to uiautomator2, Appium or another UiAutomation
   tool, run `ui helper stop` (or `session stop`).
7. Never reuse stale refs: after a navigation use the latest outline (the
   action's `screen` or a new `ui tree`).
8. Report **measured** on-screen text and tree facts separately from hypotheses.

## Boundaries

- The iOS tree is the **accessibility** hierarchy, not the SwiftUI/UIKit view tree.
  Its quality depends on the app's labelling.
- A tree with fewer than three meaningful nodes is reported with
  `sparse_accessibility_tree`. That means "the app exposes little", not "the screen
  is empty" — add `Semantics` (Flutter) or `.accessibilityLabel` /
  `.accessibilityIdentifier` (SwiftUI), or fall back to a screenshot.
- On Android, a live dump made entirely of unlabelled containers reports
  `sparse_accessibility_tree`. `--all` cannot recover labels absent from raw
  `adb exec-out uiautomator dump /dev/tty` XML. With an active Android session
  whose app is foreground on an emulator, `ui tree` confirms the sparse result
  with an unfiltered second dump. It then enables the built-in Accessibility
  Menu, force-stops and relaunches that app, and rereads the tree. The response
  includes `accessibility_recovery.attempted` and `.recovered`; if labels do not
  appear after bounded retries, it restores the settings before returning. No session,
  another foreground app, a physical device, or an offline `--dump` cannot
  trigger automatic changes. `--no-accessibility-recovery` disables this step.
  `ui accessibility enable` is the explicit alternative; `status` reads state.
  Autonom saves the original secure settings before enabling the service and
  restores them with `ui accessibility reset` or `session stop`. If settings
  change outside Autonom, reset refuses to overwrite them and retains the
  snapshot. An accessibility service can read screen content; use this only on
  an authorized test emulator. Turning it off can leave an old tree that does
  not follow navigation, so compare results with a current screenshot.
- When no element carries an identifier, `no_accessibility_identifiers` says so, so
  a zero-match `--resource-id` query is not mistaken for a missing control.
- System dialogs (permissions) need the same selectors or coordinates; on
  Android `signals.dialog` names them (`permission`, `alert`, `window`).
- Browser mirror (`android-emulator-browser`) is visual support, not a substitute
  for the tree.

## Backend recovery and explicit sessions

Keep device actions inside the Autonom CLI, including screenshots and recovery.
`--session-id <id>` binds one command to that session (its artifacts and journal)
without changing the current session. A stopped session stays readable (`journal`,
`shots`, `logs`, `report` but not `report serve`, `session show|outputs`) and refuses
everything else with `session_stopped`; another target's flags are refused with `session_target_mismatch`.
One session per machine store still applies: `session start` refuses a second one.
A stopped session's journal never grows: a command that names it is journaled in the
current session, if there is one.

On iOS, `--ui-backend auto` tries idb (its input still routed by `--ios-hid`, AXe
included) and falls back to the bundled XCUITest runner when idb's tree is empty, has
no usable root frame, or fails; the runner then serves both tree and input. An app
session with `--app-id` and full Xcode are required for the optional runner; its first
use builds and starts the helper. `--ui-backend xcuitest` selects it directly;
`--ui-backend idb` isolates idb diagnosis. The option on `session start` persists the
choice. `capabilities --probe` measures current UI readiness; without a recent read,
`ui.accessibility` describes installed tooling only.

On Android, `--ui-backend auto` uses the on-device helper and falls back to
`uiautomator dump` and adb input with `fallback_reason` when it cannot be used;
`--ui-backend helper` refuses to fall back (`android_helper_unavailable`) and
`--ui-backend uiautomator` never builds, starts or asks the helper (one still running
from an earlier call is stopped before the dump). The option on `session start`
persists the choice here too; `autonom doctor` reports `android_helper` (the JDK and
SDK tools it found, and whether their build is cached).

For duplicate labels use the observed identifier or add `--role button --mode exact`.
A `stale_ui_element` refusal happened before input: refresh the tree and reselect.
A `ui_action_uncertain` result means input may have happened: inspect the screen
before doing anything else and never automatically repeat the mutation.
A `display_geometry_unavailable` refusal must not be worked around with guessed scale.
Screenshots remain `autonom screenshot` even when the tree is empty.

Android launcher failures can be handled with
`session launch <app> --activity <component>`; normal launch now resolves the
launcher Activity automatically.
Use before/after trees or screenshots to verify the actual result. Fallback choices
and flow steps remain in the session journal. See `docs/IOS_UI_RECOVERY.md` in the
Autonom repository for implementation details and the Reader regression flows.

## Related

- `mobile-session`
- `mobile-network` — correlate on-screen errors with HTTP traffic
- `android-debugger-agent`, `ios-debugger-agent`
