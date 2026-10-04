# Reader UI recovery validation

## Port onto the AXe input routing, Xcode 27 (2026-10-04)

Checked with `tests/live/ios_ui_recovery_live.py` on macOS with Xcode 27.0
(27A266a) and an isolated iPhone Simulator (`Autonom-Recovery-Test`) running
iOS 26.5, against Settings (`com.apple.Preferences`), under a temporary
`AUTONOM_HOME`. The simulator was shut down before and after; the script
booted it and shut it down again. Two runs, both passing:

| Step | Measured result |
| --- | --- |
| Runner build | The unchanged `native/ios` project builds for testing with Xcode 27 (`AutonomUI_iphonesimulator27.0` run file, format 1). The first `ui tree` of a run, cold build cache included, took 17 to 21 s |
| Explicit `--ui-backend xcuitest` session | `ui tree` answered `ui_backend: xcuitest` with 58 nodes, every one carrying `xcuitest_ref` |
| `auto` session, idb answering an empty tree | `ui tree` answered `ui_backend: xcuitest`, `fallback_reason: empty_accessibility_tree`, 57 nodes including the General row. One `ui tap --desc General --mode exact --role button` reported `backend: xcuitest`; the next tree showed the General page (`About`) |
| Plain `--ui-backend idb` session | `ui tree` answered `ui_backend: idb` with 15 nodes, General among them |
| Runner lifecycle | After each `session stop` no `xcodebuild test-without-building` process for the UDID was left, and the process registry was empty |

The empty tree came from an `AUTONOM_IDB` wrapper that forwards every idb call
to the installed idb except `ui describe-all`. Nothing was typed, and the
runner log was not read into the report.

## Original validation (2026-09-07)

Validated on 2026-09-07 on macOS with Xcode 26.6 (17F113), an isolated iPhone 17
Simulator running iOS 26.5, and a separate Pixel 9 Android API 36 emulator.
The Reader app used fixture data; the active Reader development targets were
not used for these scenarios.

| Scenario | Measured result |
| --- | --- |
| idb returns only a zero-size application root | Forced idb reports 0 meaningful nodes. Auto selects the bundled XCUITest runner and returns 28 Reader elements, recording `empty_accessibility_tree`. |
| Coordinate tap with idb geometry 0×0 | Autonom measures 1206×2622 screenshot pixels and scale 3 from the Simulator profile, obtains 402×874 points, and dispatches through idb. A subsequent tree confirms the return to Library. |
| Offline Reader draft | All 16 flow steps pass: select fixture article, open Notebook, type draft, assert saved text, restart app, assert restored text, capture screenshots. Run `fr_fe14f994fa`. |
| System Google prompt | All 12 flow steps pass: inspect the accounts.google.com permission alert and Continue, capture a screenshot, tap Cancel, assert the prompt is gone and Reader is usable. Runs `fr_bcc9e0f4f9` and `fr_10004cef3f`. No credentials or completed sign-in. |
| Recovery provenance across CLI invocations | The final Google flow retains `ui_backend=xcuitest`, `input_backend=xcuitest`, and `fallback_reason=empty_accessibility_tree` after separate tree and flow commands. |
| Android Reader launch | Resolves `com.iatsuk.reader/me.yatsuk.reader.ReaderActivity`, launches with `am start -W`, reports `via=activity` and `mode=resume`; the tree contains the welcome screen and Google sign-in button. |
| Release archive | Contains both Python recovery modules, Swift runner, shared Xcode project/scheme and both fixture flows. Excludes build artifacts and Python caches. |

The empty-idb result was injected at the describe-all boundary on the real
isolated Simulator. The original intermittent idb failure was not assumed to
recur deterministically. The coordinate test delegated the actual tap to the
installed idb binary; all scenario UI actions and screenshots used Autonom.

Local evidence is under `build/`: `reader-geometry-tap.json`,
`reader-geometry-after.json`, `reader-flow.json`, `final-forced-zero.json`,
`final-recovery-tree.json`, `final-google-flow.json`, `android-launch.json`,
and `android-tree.json`. Session trees, screenshots, flow events and journals
are under `build/runtime-state/sessions/`. These generated files are not
committed. XCTest runner logs are private and can contain typed fixture text.

The Python regressions cover unknown geometry refusing input, recovery,
single-dispatch behavior after an uncertain response, bridge ownership,
separate session journals, stopped-session refusal, Android launch, and
independent freshness of accessibility and input evidence.

The XCUITest backend is optional and Simulator-only. The successful Reader
scenarios do not establish compatibility with every app, physical iOS device,
custom-rendered control, or authenticated Google workflow.
