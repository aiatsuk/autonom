# Flutter memory evidence

Treat Dart heap, external/graphics memory, Android Java/Kotlin heap, and native
allocations as separate budgets. Compare equivalent idle points and inspect
retaining paths. RSS or PSS growth alone is not a leak.

Useful artifacts:

- DevTools heap snapshots, class diffs, allocation traces, retaining paths
- object counts across repeated equivalent flows
- Android meminfo / HPROF for plugin, Activity, platform-view, bitmap, or JNI retention
- heapprofd when native growth is in play

Start here: <https://docs.flutter.dev/tools/devtools/memory>
