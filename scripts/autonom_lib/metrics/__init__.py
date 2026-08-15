"""Metrics: point-in-time load snapshots and directional series (Phase 4).

The package answers "how is the app right now?" cheaply and honestly:

- ``snapshot``  — one memory/CPU/disk summary per platform, with explicit
  ``metric_semantics`` and ``limitations`` (an iOS Simulator RSS is host
  process accounting, never comparable to Android PSS);
- ``series``    — N snapshots plus first/last/delta/slope math, flagging
  *directional growth leads* only — a lead is not a leak;
- ``presets``   — what heavier profilers this host can run.

Artifacts land under the session's ``metrics/`` dir; journal entries store
verbs and paths, never full dump text.
"""
