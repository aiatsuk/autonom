---
name: project-router
description: Route Flutter, native Android, native iOS, and device test/debug tasks to the smallest relevant Autonom skill set after inspecting the repository stack and runtime boundary.
---

# Project Router

## Purpose

For the whole-system map — what Autonom can do, how it works, the end-to-end
device loop — read the `autonom` skill first. This router is the dispatcher that
narrows that map to the smallest stack-specific skill set for the task at hand.

Classify the repository and task before changing code or driving a device.
A Flutter app contains `pubspec.yaml` with a Flutter SDK dependency. A native
Android project normally starts from Gradle settings and Android modules. A native
iOS project starts from `*.xcodeproj`, `*.xcworkspace`, or `Package.swift`.
Hybrid repositories can contain several, so route by the layer being changed.

For **runtime QA / UI debug** on a device, emulator, or simulator, load
`mobile-session` and `mobile-screen` (Autonom CLI) in addition to the
stack-specific debugger. Load `mobile-network` when the task mentions an API,
request, response, mock, HAR, proxy, or "why is this call failing".

Once the target app is known (a package/bundle id is in play), load
`mobile-memory` and read `~/.autonom/apps/<package>/app.md` and any matching
`flows/` runbook **before** driving the device — it carries the backend, network
model, schema locations, and step-by-step flows worked out on earlier runs, so
the same digging is not repeated. Record a new flow there only when it is
durable, reusable, and evidence-backed.

Use the repository-selected Flutter SDK for executable commands. When selection
is ambiguous or FVM is pinned, resolve it without silently falling back:

```bash
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- --version
```


## Routing workflow

1. Inspect repository documentation and the project root.
2. Run the shared toolchain snapshot when build layout or SDK state is unclear:

   ```bash
   python3 <plugin-root>/../../scripts/toolchain_snapshot.py . --execute
   ```

3. Detect the primary path:

   - Flutter feature, Dart code, widgets, navigation, tests, or package work:
     load the relevant `flutter-*` skills.
   - Android manifest, Gradle, Kotlin, platform channels, app links,
     notifications, permissions, or plugin native code inside a Flutter app:
     load `flutter-android-platform` plus the narrow Android skill.
   - Pure Kotlin or Compose project: load the native Android/Compose skills.
   - `*.xcodeproj`, `*.xcworkspace`, `Package.swift`, or work inside a Flutter
     app's `ios/` directory (plist, capabilities, Swift/Obj-C module, signing
     configuration): load `ios-project-setup`, and `ios-debugger-agent` when the
     app must actually run.
4. Load runtime skills only when device evidence is required:
   - session + screen: `mobile-session`, `mobile-screen` via `scripts/autonom.py`
   - per-app knowledge and flows: `mobile-memory` (read `~/.autonom/apps/<package>/`)
   - deeper debug: `android-debugger-agent` / `ios-debugger-agent` / `flutter-debugger-agent`
   - network inspect or mock: `mobile-network`
   - visual mirror: `android-emulator-browser`
5. When the machine's capabilities are unclear, run
   `python3 <autonom-root>/scripts/autonom.py doctor` — it reports which platforms
   are usable and what is missing, instead of discovering it mid-task.
6. Do not load every skill pre-emptively; progressive loading keeps the task
   focused.

## Default verification ladder

For Flutter:

```bash
flutter pub get
dart format --output=none --set-exit-if-changed <changed-paths>
flutter analyze <changed-paths-or-project>
flutter test <narrow-test-path>
```

For native Android:

```bash
./gradlew <narrow-task> --console=plain
```

Broaden checks when shared architecture, generated code, Gradle configuration,
platform integration, or release behavior changed.

## Boundaries

- Flutter UI behavior should be proven through widget or integration tests and
  a running Flutter app, not inferred from Android views alone.
- Native permission dialogs, platform views, notifications, and system surfaces
  can require UI Automator, Espresso, Patrol, or manual device evidence.
- Performance and memory claims need comparable runtime captures.
