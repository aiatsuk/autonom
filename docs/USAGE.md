# Usage examples

Autonom skills work the same way regardless of host agent (Codex, Claude, Grok,
…). Phrase the goal; the router and domain skills supply the procedure.

## Debug a Flutter UI regression

```text
Launch the staging Flutter target on emulator-5554, reproduce the broken yarn
write-off flow, inspect Flutter output and app-pid logcat, patch it, and replay
the same flow. Capture a screenshot and semantics/UI-tree evidence.
```

## Add and validate a platform channel

```text
Add a typed Dart wrapper and Android Kotlin implementation for the new native
action. Add Dart tests, Kotlin tests, and one device integration test including
structured error handling and activity recreation.
```

## Investigate jank

```text
Profile the exact scroll flow in Flutter profile mode on the physical device.
Report UI/raster p90, p99, worst and over-budget frames. Escalate to Perfetto or
Simpleperf only if Android/plugin work is implicated.
```

## Investigate memory growth

> **Roadmap:** Phase 4 ([`docs/plans/PHASE_4_METRICS.md`](plans/PHASE_4_METRICS.md))
> adds (1) **live session observation** — `session outputs`, `logs follow`,
> `network requests follow` — so you do not need to memorize
> `tail -f ~/.autonom/sessions/…/output/….log`, and (2) **`autonom metrics …`**
> (snapshot, series, memory capture, simpleperf/xctrace, Flutter frames).
> Until that ships:
>
> ```bash
> # live output (manual)
> tail -f ~/.autonom/sessions/<id>/output/flutter_run_mitm.log
> autonom network requests list --max 50
> autonom logs tail --package <app-id> --since 60
> ```
>
> Use the skill helpers below for Android memory/perf; host/`xctrace` on iOS.

## Investigate memory growth (current skill helpers)

```text
Repeat the open/close flow five times, return to the same idle state after each
run, compare Dart snapshots, capture Android meminfo/HPROF where indicated, and
prove or reject a retained path before patching.
```

## Release validation

```text
Validate the production flavor and target without printing secrets: format,
analyze, tests, AAB build, APK smoke install on an explicit device, app links,
background/foreground, artifact sizes, checksums, symbols, and blockers.
```

## Device UI smoke (Autonom CLI)

```text
Use Autonom mobile-session and mobile-screen: pick serial emulator-5554,
start a session, launch com.example.app, dump the compact UI tree, tap the
control labelled Continue, capture a screenshot, tail logcat for the package,
and report measured on-screen text separately from hypotheses.
```

## Drive an iOS app on the Simulator

```text
Use Autonom: list iOS simulators, start a session on the booted iPhone, install the
debug .app, launch it, dump the compact accessibility tree, tap the control labelled
Continue, take before/after screenshots, and report the measured on-screen labels
separately from hypotheses.
```

## Explain an iOS failure with evidence

```text
Reproduce the crash on the simulator: record the flow, tail logs filtered to the
bundle id, list crash reports, and pull the app's Documents/state.json into session
artifacts. Report what was measured and what remains uncertain.
```

## Put an app into a hard-to-reach state

```text
Open the deep link myapp://order/42, grant photo permission, set the location to
55.751244,37.618423, add a fixture image to the media library, then verify the
resulting screen with a UI tree and a screenshot.
```

## Check what this machine can do

```text
Run Autonom doctor and tell me which platforms are usable here, what is missing,
and the exact command to fix each gap.
```

## Generic harness prompt

```text
Use Autonom: route this repository, pick the smallest skill set, run the
narrowest useful checks first, and report measured evidence separately from
hypotheses.
```
