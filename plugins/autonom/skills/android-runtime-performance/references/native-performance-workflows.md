# Native Android performance workflows

## When to use which tool

- **Macrobenchmark** — repeatable startup and interaction numbers; keep JSON/traces.
- **Baseline Profiles** — a few critical paths; verify the shipped binary loads them.
- **Perfetto** — timeline root cause (frames, scheduler, binder, locks, I/O, GC).
- **Simpleperf** — sampled CPU stacks; not a substitute for wait analysis.
- **gfxinfo** — cheap directional frame snapshot before deeper tracing.
- **heapprofd** — native allocation stacks when managed-heap evidence is incomplete.

## Frame snapshot

```bash
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" reset
# replay one focused flow
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" framestats \
  > "$ARTIFACT_DIR/gfxinfo-framestats.txt"
```

## Simpleperf capture → report

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

## Comparison checklist

Device, Android version, refresh rate, package/variant, Flutter mode,
compilation mode, data state, thermal state, exact flow, run count. Compare only
equivalent captures; always state units and aggregation.
