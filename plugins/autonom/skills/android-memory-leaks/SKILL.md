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

## Capture

Prefer the Autonom CLI — artifacts land in the session's `metrics/` dir and
the journal keeps the timeline:

```bash
autonom metrics snapshot --label baseline        # cheap directional point
autonom metrics memory capture --label after-flow [--no-hprof]
autonom metrics memory analyze                   # series math, leads only
autonom metrics series --count 5 --interval 2    # snapshots + slope/leads
```

Pass `--no-hprof` for release/profileable builds where dumpheap is
unavailable. The standalone scripts remain for hosts without a session:

```bash
<skill-root>/scripts/capture_android_memory.sh \
  --serial <serial> --package <application-id> \
  --out-dir <artifact-dir> --label after-flow
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
