---
name: flutter-debugger-agent
description: Build, launch, attach to, inspect, and debug Flutter applications on explicit devices using Flutter machine output, Dart VM service evidence, adb, screenshots, logs, and repeatable user flows.
---

# Flutter Debugger Agent

Debug a Flutter app on one explicit device with machine-readable Flutter output
plus Android evidence when needed.

## Resolve the tool chain

```bash
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- --version
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- devices
```

Never silently fall back to a system Flutter when the repo is FVM-pinned.

## Run / attach

```bash
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- \
  run -d <device-id> --target <target> --machine
```

Capture VM service URI from machine events when you need DevTools or custom
inspectors. Prefer Autonom session + `ui` / `screenshot` / `logs` once the app
is up.

## Evidence ladder

1. Reproduce on a single pinned device/mode.
2. Screenshot + accessibility / UI tree (Autonom or adb).
3. Dart logs / VM service errors.
4. `adb logcat` filtered to the app process for platform issues.
5. After a patch, replay the same flow end-to-end.

## Rules

- One device id on every command when multiple are present.
- Debug mode is fine for functional bugs; profile mode for performance claims.
- Do not treat a green build as a green UX path — observe the screen.
