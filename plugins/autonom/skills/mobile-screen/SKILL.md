---
name: mobile-screen
description: Understand and control what is on an Android or iOS screen for AI agents using compact accessibility/UI trees, semantic find/tap, gestures, screenshots, and key/text input via the Autonom CLI.
---

# Mobile Screen (Android + iOS Simulator)

## Purpose

Answer **what is on screen** and **act on it** without relying on pixel-only guessing.

1. Prefer a **compact UI tree** (UI Automator on Android, accessibility tree via idb or XCUITest on iOS).
2. Confirm with a **screenshot** when visual layout matters or the tree is ambiguous.
3. Act with **semantic selectors** first; coordinates only as fallback.

The compact node schema is identical on both platforms, so the same reasoning works
either way. What differs is **which field carries the visible label** — see below.

## CLI

```bash
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
python3 <autonom-root>/scripts/autonom.py ui swipe --from 200,600 --to 200,200
python3 <autonom-root>/scripts/autonom.py ui key KEYCODE_BACK      # Android
python3 <autonom-root>/scripts/autonom.py ui key HOME              # iOS

# Evidence
python3 <autonom-root>/scripts/autonom.py screenshot --out /tmp/screen.png
python3 <autonom-root>/scripts/autonom.py record start --name login
python3 <autonom-root>/scripts/autonom.py record stop

# Offline parse of a dump (CI / fixtures) — the platform is detected from the file
python3 <autonom-root>/scripts/autonom.py ui tree --dump tests/fixtures/ui_dump.xml
python3 <autonom-root>/scripts/autonom.py ui tree --dump tests/fixtures/idb_describe_all_sample.json
```

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
- `ui wait --settled` says the tree was "still changing" only when its last
  snapshot was a change. If it changed and then held still for less than
  `--quiet-ms`, it says that (`stable_ms`); if only one dump fit in
  `--timeout-ms` (an Android dump can take ~2 s), it says settling could not
  be confirmed and how far to raise it.

## Which tool touched the screen (`backend`)

Every input payload (`ui tap`, `ui swipe`, `ui type`, `ui key`) names its
`backend`: `adb` on Android; `idb` or `axe` on iOS. The iOS tree always comes
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

## Gestures and keys by platform

| Verb | Android | iOS |
| --- | --- | --- |
| `ui tap`, `ui swipe`, `ui type` | yes | yes |
| `ui pinch`, `ui rotate`, `ui shake` | refused with `unsupported_on_platform` | refused with `unsupported_on_platform` |
| `ui key` | `KEYCODE_*` | `HOME`, `LOCK`, `SIDE_BUTTON`, `SIRI`, `APPLE_PAY`, or a numeric HID code |

**No platform has pinch, rotate, or shake.** Android's `input` cannot express
them and idb has no such command either, so both are refused rather than faked.
Use `ui swipe` for anything reachable by a drag; rotation and shake need a hand
on the Simulator window (Device > Rotate).

iOS has no global Back button; tap the navigation bar's back control instead.

**Typing needs focus.** `ui type` sends keystrokes to whatever has keyboard
focus; with no focused field the characters vanish while the command still
exits 0. The response carries `focused` (the node that will receive the text) with
`focus: verified` on Android or `unverified` on iOS — idb's dump has no focus
attribute, so on iOS the check degrades to "a text field is on screen" — or a
`no_focused_field` warning. Tap the field, wait for it, type, then confirm
with `ui find` on the typed text. In flows, `inputText` refuses to type into
nothing (`flow_no_focused_field`).

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
PNG itself. On iOS that is **pixels** while the tree and `ui tap` speak
points; on Android it is whatever the AVD's screen is. State the space you
computed in when you report a coordinate.

## Agent workflow

1. `session start` so the target and artifacts are explicit.
2. `ui tree` → read labels, roles, enabled state. Note which field holds the label.
3. `ui find` / `ui tap` for the next action; after a navigation, `ui wait
   --settled` before the next read.
4. `screenshot` after state changes that need visual proof — **compare before/after**;
   an exit code of 0 does not prove the screen changed. Pin the status bar first
   so the diff shows only what the app changed.
5. Re-dump the tree after navigation; never reuse stale refs.
6. Report **measured** on-screen text and tree facts separately from hypotheses.

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
- System dialogs (permissions) need the same selectors or coordinates.
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
