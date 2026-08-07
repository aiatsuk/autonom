---
name: toolchain-doctor
description: Inspect Flutter, Dart, Gradle, Android Gradle Plugin, Kotlin, SDK levels, Java, adb devices, and repository-pinned toolchain configuration without asserting stale latest versions.
---

# Toolchain Doctor

Use before project creation, SDK upgrades, Gradle migrations, broken builds, or
any claim about installed tool versions.

## Snapshot

```bash
python3 <repository-or-plugin-root>/scripts/toolchain_snapshot.py . --execute --json
```

Declarations first; optional local command execution. Never labels a version as
“latest”. Re-check `references/official-sources.md` for migrations.

## Read before editing

- `pubspec.yaml`, `.fvmrc`, `.fvm/`, `analysis_options.yaml`
- Android Gradle settings/modules, catalogs, wrapper, `gradle.properties`
- CI scripts and documented commands
- `flutter --version --machine`, `flutter doctor -v`, `dart --version`
- `./gradlew --version`, `java -version`, `adb devices -l`

Prefer `fvm flutter` when the repo is FVM-pinned and `fvm` is available.

## Policy

- Re-check official docs for version-sensitive work.
- Keep toolchain migrations separate from feature work.
- Do not paste a generic template over a live project.
- Do not upgrade the world to fix one conflict.
- Report exact observed versions and the compatibility error text.
