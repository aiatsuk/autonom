---
name: android-project-setup
description: Create, inspect, or modernize native Android modules with Kotlin, Gradle, Compose, SDK configuration, version catalogs, and compatibility checks based on the active repository and official guidance.
---

# Android Project Setup

Change the smallest coherent surface that the repository already uses.

## Workflow

1. Read settings, module builds, version catalogs, wrapper, `gradle.properties`,
   manifests, build logic, and CI.
2. Run `scripts/toolchain_snapshot.py` instead of trusting a hardcoded matrix.
3. Classify: new module, focused dependency change, or toolchain migration.
4. Keep existing dependency-management and architecture conventions when they
   are coherent.
5. Re-check official AGP / Kotlin / Gradle / library docs for version-sensitive
   work.
6. Apply a minimal patch and run the narrowest Gradle tasks that prove it.

## Defaults

- Checked-in Gradle wrapper only.
- Versions live in the existing catalog or shared build logic.
- Stable production deps unless a required API needs a preview artifact.
- Prefer KSP when supported; do not mix processor migration with unrelated work.
- `minSdk` follows product policy; `compileSdk` / `targetSdk` changes need
  platform review.
- AGP built-in Kotlin only after verifying the active AGP migration guide.

## Verify

```bash
./gradlew help --console=plain
./gradlew :app:assembleDebug --console=plain
./gradlew :app:testDebugUnitTest --console=plain
./gradlew :app:lintDebug --console=plain
```

Discover tasks with `./gradlew tasks --all`.

## Guardrails

- No dynamic dependency versions.
- No generic template pasted over a live project.
- No broad dependency upgrades during a feature-only task.
- No package rename + architecture rewrite + toolchain bump in one change
  unless the user asked for all of it.
- No “latest” claims without an observed snapshot and a dated official source.
