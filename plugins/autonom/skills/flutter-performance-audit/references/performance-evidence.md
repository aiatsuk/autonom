# Flutter performance evidence

Capture in profile mode on an explicit target. Keep the exact flow and any
integration response / timeline JSON. Report build and raster statistics
separately (p90, p99, worst, over-budget frames).

Use DevTools for frame/timeline/CPU evidence. Escalate to Android tracing when
the bottleneck is engine, binder, codec, database, or plugin/native code.

References:

- <https://docs.flutter.dev/tools/devtools/performance>
- <https://docs.flutter.dev/perf/ui-performance>
- <https://api.flutter.dev/flutter/flutter_driver/FlutterDriver/traceAction.html>
- <https://api.flutter.dev/flutter/flutter_driver/FlutterDriverExtension/enableTimelineStreams.html>
