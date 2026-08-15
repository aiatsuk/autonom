---
name: android-runtime-performance
description: Capture and interpret Android runtime performance with Macrobenchmark, Baseline Profiles, Perfetto, Simpleperf, gfxinfo, and comparable device flows while separating native and Flutter evidence.
---

# Android Runtime Performance

Measure one user-visible flow on an explicit device. Separate Flutter UI/raster
evidence from Android/native evidence.

## Choose the instrument

- **Macrobenchmark** — repeatable startup / interaction metrics on a
  profileable or release-like build.
- **Baseline Profiles** — hot paths only; confirm the shipped binary consumes
  the profile.
- **Perfetto** — frame lanes, scheduler, binder, locks, I/O, GC, custom sections.
- **Simpleperf** — sampled CPU stacks (not wall-clock waits by itself).
- **gfxinfo** — quick directional frame snapshot.
- **heapprofd** — native allocation stacks when managed-heap evidence is not enough.

Read `references/native-performance-workflows.md` before capture.

## Fast frame snapshot

Prefer the Autonom CLI inside a session — it owns reset/capture, keeps the
raw text as the artifact, and journals the run:

```bash
autonom metrics frames reset
# replay one focused flow
autonom metrics frames capture
autonom metrics trace --preset simpleperf --duration 30
```

Raw adb equivalent without a session:

```bash
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" reset
# replay one focused flow
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" framestats \
  > "$ARTIFACT_DIR/gfxinfo-framestats.txt"
```

## Simpleperf

```bash
adb -s "$SERIAL" shell rm -f /data/local/tmp/perf.data
adb -s "$SERIAL" shell simpleperf record \
  --app "$PACKAGE" -e cpu-clock -f 4000 -g \
  --duration 30 -o /data/local/tmp/perf.data
adb -s "$SERIAL" pull /data/local/tmp/perf.data "$ARTIFACT_DIR/perf.data"
<skill-root>/scripts/simpleperf_report.sh \
  "$ARTIFACT_DIR/perf.data" "$ARTIFACT_DIR" \
  --first-party-regex 'com\\.example|libapp\\.so'
```

## Heapprofd reports

```bash
<skill-root>/scripts/heapprofd_report.sh "$ARTIFACT_DIR/trace.pftrace" "$ARTIFACT_DIR"
```

## Comparison contract

Record device, Android version, refresh rate, package/variant, Flutter mode,
compilation mode, data state, thermal state, exact flow, and run count. Compare
only equivalent captures; state units and aggregation.
