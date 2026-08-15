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

```text
Repeat the open/close flow five times, return to the same idle state after each
run, compare Dart snapshots, capture Android meminfo/HPROF where indicated, and
prove or reject a retained path before patching.
```

> **Roadmap:** memory and CPU have no CLI verb yet — the work goes through the
> skill helpers above (Android meminfo/HPROF, `xctrace` on iOS). Phase 4
> ([`docs/plans/PHASE_4_METRICS.md`](plans/PHASE_4_METRICS.md)) adds
> `autonom metrics …` (snapshot, series, memory capture, simpleperf/xctrace,
> Flutter frames) plus live observation — `session outputs`, `logs follow`,
> `network requests follow` — so no one has to memorize
> `tail -f ~/.autonom/sessions/…/output/….log`. Until then, watch a run with:
>
> ```bash
> tail -f ~/.autonom/sessions/<id>/output/flutter_run_mitm.log
> autonom network requests list --max 50
> autonom logs tail --package <app-id> --since 60
> ```

## See what the app actually sent

```text
Start a consent-gated capture on the emulator, attach it, walk the checkout
flow, then list what went to the payments host, show the one that returned 500
in full, and export a HAR into the session. Report the request the app never
made separately from the ones that failed.
```

## Force a backend failure and check the UI

```text
Mock the login endpoint to return 500 with an error body, replay the login tap,
and prove from the UI tree what the user sees. Take a screenshot, note that it
was captured under an active mock, then disable the rule and confirm the real
path still works.
```

## Read a run back / hand it off

```text
Show me this session's journal: every verb, in order, with the failures. Then
list the screenshots taken under an active mock, and summarize what was
measured versus what is still a hypothesis.
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

## Turn a verified journey into a repeatable flow

```text
The login journey we just walked through works. Write it as an Autonom Flow v1
file under .autonom/flows/auth/login.yaml (secrets via ${TEST_EMAIL} and
${TEST_PASSWORD}, never inline), run `flow check` on it, then execute it with
`flow run --secret TEST_EMAIL --secret TEST_PASSWORD` and report the summary
with the events path.
```

## Record a session into a flow automatically

```text
Walk through the checkout journey on the device with autonom ui verbs
(finish with a ui find on the success screen), then run
`autonom flow create --from-session current --task checkout --out
.autonom/flows/checkout.yaml` and replay it with the command the response
suggests. Report the quality warnings, if any.
```

## Run the smoke suite and explain any failure

```text
Run `autonom flow run .autonom/flows --include-tag smoke --exclude-tag flaky`.
If a flow fails with failure_class test_failure, read its events.ndjson and
failure screenshot and tell me what the app actually showed; do not retry the
flow.
```

## Watch a session live

```text
Run `autonom session outputs` and tell me what is followable. While I drive the
app, stream errors with
`autonom logs follow --source device --grep 'Exception|Error' --max-seconds 60`
and new API calls with `autonom network requests follow --max-seconds 60`.
Every follow must be bounded (--max-seconds / --max-lines); to watch in my own
terminal give me the `shell_hint` (`tail -f <abs_path>`) from session outputs.
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
