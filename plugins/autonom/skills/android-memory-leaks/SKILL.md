---
name: android-memory-leaks
description: Investigate Android Java, Kotlin, native, and process memory using repeatable flows, meminfo, heap dumps, retaining paths, heapprofd, and before-after evidence without confusing growth with proof.
---

# Android Memory Leaks

Growth is a lead. A leak is a retained object with a root path, or a repeatable
accumulation under an equivalent flow.

## Evidence ladder

1. Directional snapshots via `dumpsys meminfo` and `/proc/<pid>/status`.
2. Managed-heap dump when the process is debuggable and the suspect lives in
   Java/Kotlin heap.
3. Class counts, retained size, fields, and GC roots (Studio Profiler,
   LeakCanary, Shark).
4. Perfetto `heapprofd` for native allocation growth.
5. Same flow again after the fix; same metrics and paths.

## Capture helper

```bash
<skill-root>/scripts/capture_android_memory.sh \
  --serial <serial> \
  --package <application-id> \
  --out-dir <artifact-dir> \
  --label after-flow
```

Requires an explicit serial when multiple devices are up. Sanitizes artifact
names, gathers several process views, and removes temporary device files. Pass
`--no-hprof` for release/profileable builds where dumpheap is unavailable.

Series analysis (directional only):

```bash
python3 <skill-root>/scripts/analyze_meminfo_series.py <artifact-dir> \
  --glob '*-meminfo.txt' --json
```

## Interpretation rules

- One spike ≠ leak.
- Prove managed leaks with a retaining path or repeatable accumulation.
- Separate Dart/Flutter heap from Android managed and native memory.
- Separate intentional process caches from feature-lifetime objects.
- Fix the retaining edge; do not sprinkle forced GC or blanket clear calls.

## Frequent retainers

Activities, fragments, platform views, contexts, listeners, coroutines,
adapters, bitmaps, singletons, plugin registrars, event channels, JNI handles,
and caches without clear ownership.
