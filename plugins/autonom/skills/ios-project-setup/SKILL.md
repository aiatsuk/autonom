---
name: ios-project-setup
description: Inspect an iOS project's real layout for AI agents — resolve workspace vs project, schemes, configurations, bundle identifiers, simulator destinations, and the built .app path without hard-coding versions.
---

# iOS Project Setup

## Purpose

Establish **what this repository actually builds** before running anything. Every
value below is discovered from the project, never assumed. Do not assert a "latest"
or "recommended" Xcode, Swift, or deployment-target version; report what is
declared.

## 1. Toolchain, as installed

```bash
xcode-select -p
xcodebuild -version
xcrun simctl list runtimes
python3 <autonom-root>/scripts/autonom.py doctor
```

## 2. Workspace or project

```bash
ls *.xcworkspace *.xcodeproj Package.swift 2>/dev/null
```

| Found | Build with |
| --- | --- |
| `*.xcworkspace` | `-workspace <name>.xcworkspace` — required when CocoaPods is used |
| `*.xcodeproj` only | `-project <name>.xcodeproj` |
| `Package.swift` | `swift build` / `swift test` |
| `ios/` inside a Flutter app | drive Flutter; touch Xcode only for native/platform work |

A Flutter repository has `pubspec.yaml` with a Flutter SDK dependency and an `ios/`
directory containing `Runner.xcworkspace`. Route Dart work to the `flutter-*`
skills; use this skill for signing, capabilities, plist, or native module work.

## 3. Schemes, configurations, destinations

```bash
xcodebuild -list -workspace <name>.xcworkspace
xcodebuild -showBuildSettings -scheme <Scheme> | grep -E 'PRODUCT_BUNDLE_IDENTIFIER|PRODUCT_NAME|IPHONEOS_DEPLOYMENT_TARGET|CONFIGURATION_BUILD_DIR'
xcrun simctl list devices available
```

Take the bundle id from `PRODUCT_BUNDLE_IDENTIFIER` rather than reading `Info.plist`
directly — the plist usually contains `$(PRODUCT_BUNDLE_IDENTIFIER)`, and per-flavor
schemes can differ.

Destination for a simulator build:

```text
-destination 'platform=iOS Simulator,id=<UDID>'      # exact, preferred
-destination 'platform=iOS Simulator,name=iPhone 17 Pro'
```

Prefer the UDID from `autonom devices --platform ios`: a name can match several
runtimes.

## 4. Locate the built `.app`

```bash
# Flutter
build/ios/iphonesimulator/Runner.app

# xcodebuild with an explicit derived-data path
build/DerivedData/Build/Products/<Configuration>-iphonesimulator/<PRODUCT_NAME>.app
```

Pass that path to `session start --install`. Storing it in the session is what lets
`session clear` do a full uninstall + reinstall later.

## 5. Flavors and configurations

Multi-flavor apps usually carry one scheme per flavor and per-configuration
`PRODUCT_BUNDLE_IDENTIFIER` values. Confirm which scheme maps to which bundle id
with `-showBuildSettings` before installing, so the session's `--app-id` matches
what was actually built.

## Accessibility readiness

`ui find` selects by label and identifier. Before a UI-automation pass, check the
app exposes them:

- SwiftUI — `.accessibilityLabel("Log In")`, `.accessibilityIdentifier("login_button")`
- UIKit — `accessibilityLabel`, `accessibilityIdentifier`
- Flutter — `Semantics(label: ..., identifier: ...)`

An identifier is worth more than a label: it survives localization, so a selector
built on it does not break when the app is translated.

## Rules

- Never hard-code an Xcode, Swift, or iOS version as "current".
- Never modify signing settings, certificates, provisioning profiles, or
  entitlements as a side effect of a build or test task.
- Keep secrets out of logs: no keychain contents, API keys, or `.env` values.
- Prefer the repository's existing build entry point over inventing a new one.

## Related

- `ios-debugger-agent` — build, run, and gather evidence
- `mobile-session`, `mobile-screen`
- `project-router` — decide whether iOS skills are needed at all
