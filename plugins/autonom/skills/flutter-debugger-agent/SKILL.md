---
name: flutter-debugger-agent
description: Build, launch, attach to, inspect, and debug Flutter applications on explicit devices using Flutter machine output, Dart VM service evidence, adb, screenshots, logs, and repeatable user flows.
model: inherit
effort: high
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

## Change code and check it: hot reload, hot restart, rebuild

When you edit the app and check the result on the device, keep one debug
`flutter run` alive for the whole task. Any Flutter app in a debug build can
do this; no special build is needed. A full build costs minutes, a hot reload
a second or two.

```bash
# once, in the background; the pid file is how you reach it later
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- \
  run -d <device-id> --target <target> --pid-file /tmp/flutter-<app>.pid

kill -USR1 "$(cat /tmp/flutter-<app>.pid)"   # hot reload
kill -USR2 "$(cat /tmp/flutter-<app>.pid)"   # hot restart
```

With `--machine` send the daemon command on the process's stdin instead:
`[{"id":1,"method":"app.restart","params":{"appId":"<appId>","fullRestart":false}}]`
(`fullRestart: true` is a hot restart; `appId` comes from the `app.start`
event). An app that is already installed and running in debug takes the same
signals after `flutter attach -d <device-id> --pid-file /tmp/flutter-<app>.pid`.

| You changed | Do | What happens to the app |
| --- | --- | --- |
| Dart code inside widgets, methods, styles | hot reload | state and the current screen stay |
| `main()`, `initState()`, global or static initialisers, enum or generic type shapes | hot restart | the app starts again: in-memory login, navigation and in-app overrides are gone |
| native code, Gradle or Xcode settings, the manifest or `Info.plist`, a new plugin with native code | stop, then `flutter run` again | a full build and install |

- Read what `flutter run` printed after the signal: a compile error keeps the
  old code running, and "Reloaded 0 libraries" means the edit was not picked up.
- Then check the screen itself, waiting inside the call:
  `autonom ui wait --text "<the new label>"`.
- After a hot restart check that the app is alive and walk back to the screen
  (`autonom ui do` with the steps you already know) before judging the change.
- Hot reload and hot restart are debug-mode tools. Measure performance in
  profile mode, which has neither.

## Evidence ladder

1. Reproduce on a single pinned device/mode.
2. Screenshot + accessibility / UI tree (Autonom or adb).
3. Dart logs / VM service errors.
4. `adb logcat` filtered to the app process for platform issues, read in bounded
   slices: `autonom logs tail --package <id> --max-lines 200 --grep '<pattern>'`, or
   `adb logcat --pid <pid> -t 300`. Never leave an unbounded stream running.
5. After a patch, replay the same flow end-to-end.

## Rules

- One device id on every command when multiple are present.
- Debug mode is fine for functional bugs; profile mode for performance claims.
- Do not treat a green build as a green UX path — observe the screen.
