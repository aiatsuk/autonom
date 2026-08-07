---
name: android-debugger-agent
description: Build, install, launch, inspect, and debug Android apps on explicit adb targets using Gradle or Flutter builds, screenshots, robust UI Automator selectors, input events, and focused logcat evidence.
---

# Android Debugger Agent

Drive a native or hybrid Android app on one explicit adb target. Prefer
Autonom CLI when a session is active; fall back to the helpers below when you
need raw adb or offline XML analysis.

## 1. Pin a single device

```bash
adb devices -l
emulator -list-avds
```

If more than one target is connected, pass `-s <serial>` on every adb call.

## 2. Build and install

Discover the real Gradle task; do not invent module names.

```bash
./gradlew tasks --all
./gradlew :app:installDebug --console=plain
```

Flutter apps: use `flutter-debugger-agent` for the Dart/engine path and keep
this skill for the Android shell layer. Stop at the first actionable build error.

## 3. Launch and capture state

```bash
PACKAGE=<application-id>
ACTIVITY="$(adb -s <serial> shell cmd package resolve-activity --brief "$PACKAGE" | tr -d '\r')"
adb -s <serial> shell am start -W -n "$ACTIVITY"
adb -s <serial> exec-out screencap -p > /tmp/android-screen.png
adb -s <serial> exec-out uiautomator dump /dev/tty > /tmp/android-ui.xml
```

## 4. Selectors and taps

```bash
python3 <skill-root>/scripts/ui_tree_summarize.py /tmp/android-ui.xml
python3 <skill-root>/scripts/ui_query.py /tmp/android-ui.xml --text Settings --json
python3 <skill-root>/scripts/ui_query.py --serial <serial> \
  --desc "Open settings" --wait 10 --click
```

Supported selectors: text, content-desc / Flutter semantics, resource-id, class,
package, booleans, exact / contains / regex, index, live wait. Ambiguous
matches must be narrowed or indexed — never silently tap the first hit.

## 5. Logs

```bash
adb -s <serial> logcat -c
PID="$(adb -s <serial> shell pidof -s "$PACKAGE" | tr -d '\r')"
adb -s <serial> logcat --pid "$PID"
adb -s <serial> logcat -b crash -d
```

## 6. Replay

After a fix, reuse the same target, setup, inputs, screenshot, query, and logs.

## Testing note

Compose test tags and Flutter semantics belong in automated tests. UI Automator
is for system UI and black-box evidence, not a substitute for widget tests.
