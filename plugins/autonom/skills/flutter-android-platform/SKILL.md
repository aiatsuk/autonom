---
name: flutter-android-platform
description: Implement and debug the Android platform layer of Flutter apps including Gradle, manifests, flavors, permissions, platform channels, plugins, app links, notifications, and native lifecycle behavior.
---

# Flutter Android Platform

Own the Android shell of a Flutter app: Gradle, manifests, flavors, plugins,
channels, links, notifications, and lifecycle.

## Inspect first

- `android/` Gradle files, flavors, application ids, signing config location
- manifests and merged manifest output
- plugin registrations and GeneratedPluginRegistrant assumptions
- platform channels and method/event APIs
- deep links / App Links, notification intents, launch modes

Use `toolchain-doctor` / `toolchain_snapshot.py` for SDK and AGP pins.

## Common work

1. Keep Flutter Gradle plugin and Android embedding conventions the project
   already uses.
2. Add permissions and queries only with a product reason; document why.
3. Implement channel handlers with explicit error codes and main-thread rules.
4. Verify deep links with `adb` VIEW intents and App Link verification where
   required.
5. Confirm flavor × build-mode matrix before release packaging.

## Debug loop

```bash
flutter build apk --debug --flavor <flavor>
adb -s <serial> install -r <apk>
adb -s <serial> shell am start -W -n <package>/<activity>
adb -s <serial> logcat --pid "$(adb -s <serial> shell pidof -s <package> | tr -d '\r')"
```

## Boundaries

- Do not “fix” Flutter UI bugs by rewriting Android unless evidence points
  there.
- Do not print keystore secrets or signing material.
- Prefer repository scripts for flavors and dart-defines.
