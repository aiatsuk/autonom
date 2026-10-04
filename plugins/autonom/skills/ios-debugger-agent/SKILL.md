---
name: ios-debugger-agent
description: Build, run, and debug iOS apps on the Simulator for AI agents — resolve the scheme and destination, install and launch, then climb an evidence ladder through accessibility trees, screenshots, logs, and crash reports.
---

# iOS Debugger Agent (Simulator)

## Purpose

Turn "the iOS app misbehaves" into measured evidence. Build with the project's own
toolchain, drive the app through the Autonom CLI, and separate what was observed
from what is inferred.

Physical devices are out of scope: they need Developer Mode, signing, and tunnels.

## Prerequisites

```bash
python3 <autonom-root>/scripts/autonom.py doctor
```

Everything except `ui *` works with Xcode alone. The accessibility tree
additionally needs `idb` plus `idb_companion`; input (tap, swipe, type, key)
needs a companion that can load SimulatorKit, or AXe. `doctor` reports
`capabilities.ios_tree` and `capabilities.ios_hid` separately and prints the
exact fix for each gap.

**Xcode 27 trap:** SimulatorKit moved to `Contents/SharedFrameworks`, and an
`idb_companion` built before that looks in `Library/PrivateFrameworks`. The
tree still works, but every input fails with `ios_hid_framework_missing`.
Fix it with `brew update && brew upgrade idb-companion`, or install AXe
(`brew install cameroncooke/axe/axe`): the default `--ios-hid auto` then
routes input through AXe while the tree stays on idb, and each `ui` input
payload reports `backend: axe`. Pin a route with `--ios-hid idb|axe` (or
`AUTONOM_IOS_HID`) before the verb. Do not report a failed tap as an app
defect until `doctor` shows input ready. On Xcode 27 with AXe carrying input,
`capabilities.ios_hid.ready` stays `false` (it describes idb's own HID) while
`ios_hid.axe_ready` is `true`, `ios_hid.backend` is `axe`, and
`capabilities.ios_ui.ready` is `true`: read `ios_ui` and `axe_ready`, and keep
going — input works.

**When idb's tree is unusable** (empty, a zero-size root, or idb failing),
the default `--ui-backend auto` switches an app session to the bundled
XCUITest runner; results then say `ui_backend: xcuitest` and the
`fallback_reason`. See "Backend recovery and explicit sessions" below.

## 1. Build

Use whatever the repository already uses — never introduce a second build path.

```bash
# Flutter
flutter build ios --simulator --debug
# -> build/ios/iphonesimulator/Runner.app

# Xcode project or workspace
xcodebuild -scheme <Scheme> -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath build/DerivedData build
# -> build/DerivedData/Build/Products/Debug-iphonesimulator/<App>.app

# SwiftPM executable targets
swift build
```

Resolve the scheme from `xcodebuild -list` rather than guessing. When FVM or a
pinned Flutter SDK is present, resolve it with `scripts/flutter_exec.py` instead of
falling back to a system SDK.

## 2. Own a session

```bash
python3 <autonom-root>/scripts/autonom.py devices --platform ios
python3 <autonom-root>/scripts/autonom.py session start \
  --platform ios --target <UDID> \
  --install <path>.app --launch --app-id <bundle-id> --log-stream
```

`--log-stream` starts a background `log stream` for the session, filtered to
the app and capped on disk (`AUTONOM_IOS_LOG_MAX_MB`, default 50, one
rotation), which makes `logs tail` cheap afterwards. Without it, tailing
falls back to `log show`. A second `session start` while one is live is
refused with `session_already_active`; stop the old one first.

## 3. Reproduce and capture

```bash
python3 <autonom-root>/scripts/autonom.py ui tree
python3 <autonom-root>/scripts/autonom.py ui find --desc "Log In" --mode exact
python3 <autonom-root>/scripts/autonom.py screenshot --out /tmp/before.png
python3 <autonom-root>/scripts/autonom.py ui tap --desc "Log In" --mode exact
python3 <autonom-root>/scripts/autonom.py screenshot --out /tmp/after.png
python3 <autonom-root>/scripts/autonom.py logs tail --package <bundle-id> --since 60
python3 <autonom-root>/scripts/autonom.py crash list --app-id <bundle-id>
```

**Compare the two screenshots.** A tap that exits 0 has not been proven to do
anything; a changed screen (or a changed tree) is the proof. Pin the status
bar first (`simulator status-bar pin`) so the battery and signal glyphs do not
show up as a difference, and pin the keyboard before typed text must be exact:

```bash
python3 <autonom-root>/scripts/autonom.py simulator status-bar pin
python3 <autonom-root>/scripts/autonom.py simulator keyboard pin --value locale=en-US --value reboot=true
```

The keyboard pin writes the simulator's preference store (autocorrect,
prediction, auto-capitalisation off; locale set) and needs the device shut
down — `reboot=true` cycles it for you. Undo with `keyboard reset` and
`status-bar clear`.

## 4. Drive state the UI cannot reach

```bash
python3 <autonom-root>/scripts/autonom.py open "myapp://order/42"
python3 <autonom-root>/scripts/autonom.py permissions grant photos <bundle-id>
python3 <autonom-root>/scripts/autonom.py permissions reset all <bundle-id>
python3 <autonom-root>/scripts/autonom.py location set 55.751244,37.618423
python3 <autonom-root>/scripts/autonom.py media add fixtures/photo.png
python3 <autonom-root>/scripts/autonom.py file ls Documents --app-id <bundle-id>
python3 <autonom-root>/scripts/autonom.py file pull Documents/state.json --app-id <bundle-id>
python3 <autonom-root>/scripts/autonom.py session launch <bundle-id> --setenv FLAVOR=staging
python3 <autonom-root>/scripts/autonom.py simulator biometric enroll     # then match | nonmatch | unenroll
```

`simulator biometric match|nonmatch` posts the Face ID notification on a
Face ID device and the Touch ID one otherwise (both when the device type
does not say; `biometry` reports which), through the Simulator's Darwin
notifications. Nothing reports whether a prompt consumed it, so it answers
`verified: false` with the reason in `verification`; prove the effect from
the app's screen. Unknown
actions are `invalid_simulator_action` with the valid list.

File access is confined to the app container; a path that escapes it is refused.
Pulled files are written into session artifacts and their contents are not echoed,
because app data can contain personal information.

## 5. Evidence ladder

1. code inspection;
2. narrow unit / XCTest;
3. explicit-simulator integration flow;
4. screenshot + accessibility tree + logs + crash reports;
5. recording of the exact repro (`record start` / `record stop`);
6. equivalent before/after replay after the fix.

Report measured facts, code-backed findings, hypotheses, and remaining uncertainty
as four separate categories.

## Boundaries and honesty rules

- The tree is the **accessibility** hierarchy. A Flutter view without `Semantics`
  can be nearly empty; that is reported as `sparse_accessibility_tree` and means
  the app exposes little, not that the screen is blank.
- Clear app state with `session clear` (uninstall + reinstall). `--strategy privacy`
  resets permissions **only** and leaves data in place.
- Load questions have CLI verbs: `autonom metrics snapshot` (host `ps` view of
  the Simulator process + data-container size — never comparable to Android
  PSS), `metrics series` for direction, `metrics memory warn` as a best-effort
  pressure stimulus, and `metrics trace --preset allocations|time-profiler|
  leaks|hitches` for real Instruments `.trace` bundles. `hitches` is
  physical-device only: Instruments refuses Animation Hitches on the
  Simulator (`list-presets` says `unsupported_on_simulator`), so on the
  Simulator use `time-profiler` for main-thread stalls or
  `metrics frames flutter-summary` on Flutter frame timings. Do not present a
  screenshot or a log line as a performance measurement.
- Simulator behavior differs from a physical device for camera, sensors, push,
  background execution, and memory pressure. Say so when it matters to the finding.

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

- `mobile-session`, `mobile-screen`
- `mobile-network` — inspect and mock the app's HTTP(S) traffic
- `ios-project-setup` — scheme, simulator, and bundle-id conventions
- `flutter-debugger-agent` — Dart-side debugging for Flutter apps
