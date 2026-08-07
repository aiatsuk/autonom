---
name: flutter-memory-leaks
description: Investigate Flutter and Android memory growth using Dart retaining paths, DevTools snapshots, disposal and lifecycle review, native heap evidence, and comparable before-after flows.
---

# Flutter Memory Leaks

Separate Dart heap, external/graphics memory, Android managed heap, and native
allocations. Compare equivalent idle points.

## Ladder

1. Define one flow and an equivalent idle baseline.
2. Capture DevTools heap snapshots (or allocation tracing) before and after.
3. Inspect retaining paths for growing types.
4. Review disposal: controllers, streams, timers, listeners, `BuildContext`
   capture, image caches, statics, platform-channel subscriptions.
5. Escalate to `android-memory-leaks` (and `analyze_meminfo_series.py`) when
   process PSS/graphics/activity growth is not explained by Dart snapshots.
6. Fix the retaining edge; repeat the same flow.

Read `references/memory-evidence.md`.

## Rules

- RSS/PSS growth alone is not proof.
- Disposed ≠ unreachable; check the path.
- Do not paper over leaks with forced GC or global cache wipes.
- Add leak-tracking libraries only if the repo already uses them or the user
  asks.

## Report

Flow, device, mode, artifacts, retained types, path or accumulation proof,
fix, before/after.
