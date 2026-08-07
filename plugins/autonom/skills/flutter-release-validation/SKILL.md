---
name: flutter-release-validation
description: Validate Flutter Android release candidates across flavors, targets, Dart defines, signing boundaries, analysis, tests, APK or app bundle builds, smoke flows, and app-size artifacts.
---

# Flutter Release Validation

Lock the exact flavor, target, application id, version, dart-defines, signing
source, backend environment, and output artifact before building.

## SDK selection

```bash
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- --version
```

Respect FVM pins; no silent system fallback.

## Ladder

1. Clean only if stale build state is plausible.
2. Resolve deps / codegen via repo commands.
3. Format, analyze, release-critical tests.
4. Build the requested artifact.
5. Install the closest installable variant on an explicit device.
6. Smoke: startup, auth/bootstrap, navigation, network failure, deep links,
   background/foreground, one critical transaction.
7. Record artifact paths, sizes, mapping/symbols, checksums when required.

## Typical commands

```bash
flutter analyze
flutter test
flutter build appbundle --release --flavor <flavor> --target <target> \
  --analyze-size
```

Use APK for local smoke when the deliverable is an AAB. Prefer project scripts
for `--dart-define-from-file`.

## Signing

Never print keystore material or service-account JSON. Report only which signing
source was used and whether verification succeeded.

## Report

Commit, toolchain snapshot, flavor/target/mode, commands, tests, artifacts,
device smoke, blockers, untested areas.
