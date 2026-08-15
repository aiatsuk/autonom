---
name: flutter-performance-audit
description: Audit Flutter runtime performance from code and profile evidence, separating UI-thread build cost, raster cost, startup, memory pressure, image work, and native Android bottlenecks.
---

# Flutter Performance Audit

Separate UI-thread build cost from raster cost. Measure in **profile mode** on
an explicit device.

## Evidence order

1. One user-visible flow and symptom.
2. Code pass: rebuild scope, sync Dart work, list identity, images, layout/paint,
   state churn, isolate boundaries.
3. Profile-mode capture on a named device.
4. DevTools frames / timeline / CPU / raster / custom `dart:developer` events.
5. For repeatable flows: `integration_test` `watchPerformance` or `traceAction`;
   keep the JSON.
6. Smallest fix; equivalent re-capture.

Read `references/performance-evidence.md` first.

## Commands

```bash
flutter run --profile -d <device-id> --target <target>
flutter test integration_test/performance_test.dart -d <device-id>
autonom metrics frames flutter-summary build/integration_response_data.json
```

(`<skill-root>/scripts/frame_timings_summary.py` is the same math for hosts
without the Autonom CLI.)

## Metrics

Report build and raster average, p90, p99, worst, and over-budget frames. State
the budget (refresh rate). Keep startup and app size as separate claims.

## Boundaries

- Debug timings are not representative.
- Emulator results are directional.
- Flutter UI/raster evidence does not replace Perfetto/Simpleperf for Android,
  binder, codec, DB, or native plugin bottlenecks.
- Compare same device, thermal state, mode, target, data, and flow.
