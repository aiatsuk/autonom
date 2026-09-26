# Autonom capability matrix

Target: **universal mobile test/debug harness for AI agents** (Android + iOS).

Legend: ✅ shipped · ⚠️ partial · 🔜 planned · ❌ not planned for near term

| Capability | Android | iOS Simulator | Notes |
| --- | --- | --- | --- |
| Agent-portable skills (Codex / Claude / Grok) | ✅ | ✅ | install via marketplace or `install_skills.sh` |
| Unified device listing | ✅ | ✅ | `autonom devices`; each entry has a `running` flag |
| Boot / shut down a target | ✅ | ✅ | `devices boot --avd`/`--udid`, `devices shutdown`; refuses hardware |
| Bootable AVD discovery | ✅ | — | `devices` reports an `avds` array on Android, plus `avd_profiles` (hardware profile, screen, density, API) and the `avd` a running emulator booted from |
| Explicit multi-target selection | ✅ | ✅ | `--platform` / `--target`; `--serial` and `--udid` are aliases |
| Guided first run | ✅ | ✅ | `autonom tour` — what the harness has, the workflow, this Mac's targets, and an offer to boot one and walk three screens into Settings with per-step screenshots, hierarchies and logs, an HTML report and a written account (`--run`, `--human`) |
| Environment diagnosis | ✅ | ✅ | `autonom doctor` — tools, capabilities (`ios_tree` and `ios_hid` separately: the tree needs idb, input needs a companion that loads SimulatorKit or AXe), orphans, every active `AUTONOM_*` override (`override_path_missing`), and emulators still routed through another session's live proxy (`device_attached_to_foreign_proxy`); `--strict` exits 1 with `ok: false` and the `strict_failures` list |
| Session + artifact dirs | ✅ | ✅ | machine-global `~/.autonom/sessions/<id>/`; `autonom session *`; `session start` refuses with `session_already_active` while one is live, reports what `--install`/`--launch` did (`installed`, `launched` with the launch report), and a failed `--install`/`--launch` rolls the new session back (`session_rolled_back`); `session stop` also stops every registered process the session owns (`process_teardown`) and names an idb companion it did not start (`companion_left_running`) |
| Session journal (actions + notes) | ✅ | ✅ | `journal.ndjson`; `autonom journal` / `note`; secret-safe |
| Boot / install / launch / terminate | ✅ | ✅ | simulator boots automatically on session start; an Android resume launch is `am start -W` on the launcher activity (`component`, `launch` report; `monkey` only when no launcher activity resolves) |
| Clear app data | ✅ | ⚠️ | Android `pm clear`; iOS uninstall+reinstall, or `--strategy privacy` for permissions only |
| Compact UI / accessibility tree | ✅ | ✅ | UIAutomator; idb `describe-all`; one node shape on both platforms (`long_clickable`, `checkable` included); live trees carry `screen` (the bounds' coordinate space) and `truncated` exactly when `--max-nodes` cut the list; a node that cannot be on screen (iOS lists Flutter's scroll cache at a 0x0 frame) stays in the tree marked `visible: false`, live or `--dump`; `--format outline` prints one indented line per node, `--interactable` keeps only nodes an agent can act on (focusable-only labels are not; long-press-only and checkable controls are) |
| Wait for a settled screen | ✅ | ✅ | `ui wait --settled` — polls until two consecutive trees match for `--quiet-ms`, bounded by `--timeout-ms`; `settled`, `snapshots`, `changes`, `elapsed_ms`, `dump_ms`; exit 1 when it never settled, and the warning says "still changing" only when the last snapshot was a change — otherwise that the tree changed and then held still for less than `--quiet-ms`, or that settling could not be confirmed in the budget (an Android dump can take ~2 s); `stable_ms` says how long it held still |
| Semantic find / tap | ✅ | ✅ | text, desc/label, resource-id / accessibility identifier, whitespace-normalised (iOS joins a merged label with a newline, Android with a space); an empty selector is `selector_required`; `ui tap` and `ui find` resolve on-screen matches first and count `--index` among them, as a flow does; `ui tap` on a node never laid out on screen is `element_offscreen` and taps nothing; `ui find --all` lists on-screen matches first, and every match `ui find` reports carries the `index` that selects it; a `--text` miss whose value matches a `desc` says so on both platforms (iOS and Flutter-on-Android labels live in `desc`) |
| iOS input through AXe | — | ✅ | tap/swipe/type/key go through idb, or AXe when idb's HID cannot load SimulatorKit (Xcode 27 with an old companion); `--ios-hid auto\|idb\|axe` / `AUTONOM_IOS_HID`, `--axe PATH` / `AUTONOM_AXE`; every input payload names its `backend` |
| Gestures | ⚠️ | ⚠️ | tap/swipe on both; `ui pinch\|rotate\|shake` have no backend on either and are refused — see below |
| Text and hardware keys | ✅ | ✅ | Android `KEYCODE_*`; iOS `HOME`/`LOCK`/`SIRI`/`SIDE_BUTTON`; `ui type` warns `no_focused_field` when nothing on screen has keyboard focus |
| Screenshot | ✅ | ✅ | iOS uses `simctl`, so it works without idb |
| Screenshot provenance | ✅ | ✅ | metadata embedded in the PNG; shots taken under an active mock are flagged `screenshot_shows_mocked_data` |
| Screenshot index / browse | ✅ | ✅ | `autonom shots list [--task --grep --mocked-only]`, `shots show <path>` |
| Screenshot dimensions | ✅ | ✅ | `width`/`height` read from the PNG header on `screenshot`, `shots show`, and the index — the capture's coordinate space, stated rather than guessed |
| Deterministic status bar | ✅ | ✅ | `simulator status-bar pin` — full battery and signal, no notification icons, the real ticking clock. iOS: `simctl status_bar override`, read back with `status_bar list` for `verified`. Android: *live* mode (battery service, emulator `gsm signal-profile` plus an explicit `gsm signal` RSSI, `cmd statusbar send-disable-flag`) because SystemUI demo mode freezes the clock; the signal cannot be read back and the emulator's modem and SystemUI drift within a minute, so each live pin carries a `signal_unstable` warning and is re-asserted right before every `screenshot` and flow capture (`repinned: true` in the payload/event). The record is bound to the emulator's AVD name and boot id: another AVD (or a reboot) on the same serial is never sent the pin nor restored from its snapshot — the record is dropped (`stale_pin_dropped: true` plus a warning), and `devices shutdown`/`devices boot` drop it outright. Because an emulator quickboots from the snapshot `emu kill` saves, `devices shutdown` (and the tour) first restores a status-bar and animations pin recorded on that same emulator exactly as `clear`/`reset` would (`pins_restored: ["status-bar", "animations"]`); a record from another AVD or boot is not restored (`stale_pin_dropped` warning), and a failed restore warns `pin_restore_failed` without stopping the shutdown. On API 36 emulators SystemUI may not draw the battery glyph at all although `dumpsys battery` reports the pinned level. `hhmm=0941`, `wifi=`, `mobile=`, `mode=demo`, and `override` use demo mode knowingly. `clear` undoes both |
| Simulator control verification | ✅ | ✅ | every `simulator` control reports `verified` (the state was read back and matches) and `verification` (`read_back`, `mismatch`, or why it could not be checked); an action the control does not have is `invalid_simulator_action` with the valid list, refused before anything is sent. iOS reads back the status bar and battery (`status_bar list`), biometric enrollment (`notifyutil -g`) and the pasteboard; `clipboard get` reads the iOS pasteboard, while Android has no host-level clipboard read (`unsupported_on_platform`) and an Android build without `cmd clipboard` refuses `clipboard set` (`unsupported_capability`); `push send` takes its `app_id` from the session when it is left out |
| System animations off | ✅ | ❌ | `simulator animations pin\|reset\|show` — the three Android animation scales to 0, `reset` restores what the first pin replaced; iOS has no host-level switch and refuses with `unsupported_capability` |
| Keyboard / locale pinning | ❌ | ✅ | `simulator keyboard pin\|reset\|show` — autocorrect, prediction, and auto-capitalisation off, locale set, on the shut-down simulator's preference store (`reboot=true` cycles it and starts the session's `--log-stream` again, `log_stream_restarted`); `pin` snapshots what it replaces and `reset` restores it. Android keeps these inside Gboard; refused with `unsupported_capability` |
| Screen recording artifact | ✅ | ✅ | `autonom record start\|stop` |
| Logs | ⚠️ | ⚠️ | logcat; `log stream`/`log show`. Android `--package` narrows by the app's uid on API 31+ (plus the lifecycle lines naming it), else by its running pid, else to lines naming it, and says which in `filter` (`log_filter_degraded` when it is not the uid); an unknown package is `app_not_installed`. iOS matches the installed app's bundle path (every Flutter app's executable is `Runner`) and reports the `executable`. The iOS `--log-stream` file is capped on disk (`AUTONOM_IOS_LOG_MAX_MB`, one rotation), registered to the session, and started again after a `simulator keyboard pin` reboot or a `session clear --strategy reinstall` (`log_stream_restarted`, or a `log_stream_stopped` warning) |
| Crash reports | ⚠️ | ✅ | Android: crash logcat buffer; iOS: idb crash store, and a listing that failed (idb lost its companion) is an error, never an empty list |
| Deep links | ✅ | ✅ | `autonom open <url>`; Android sends the VIEW intent with `am start -W` and reports `handled_by`, the activity that took the URL (`url_opened_chooser` when it is the system chooser, `url_not_handled` when nothing took it); iOS `simctl openurl` cannot say, so `handled_by: "unknown"` |
| Permissions | ✅ | ✅ | `pm grant/revoke/reset`; `simctl privacy`, the service checked against what this Xcode's `simctl help privacy` lists |
| Simulated location | ⚠️ | ✅ | set: iOS simctl / Android emulator `geo fix`; `location get` reads the system's last fix on Android and reports `requested` + `delivered` against what the session set (iOS has no read-back) |
| Media library seeding | ✅ | ✅ | `autonom media add` |
| App-container file access | ✅ | ✅ | `autonom file ls\|pull`, confined to the container; a release/system app refuses with `app_not_debuggable` |
| Remote target host | — | ✅ | idb client can drive a companion on another Mac: `--idb-host/--idb-port` or `AUTONOM_IDB_COMPANION` add `--companion host:port` to every idb call |
| Emulator browser mirror | ✅ | ⚠️ | `canvas serve` (`android-emulator-browser` skill). iOS: frames are polled `simctl io screenshot` PNGs (no H.264 stream; the `screenrecord` transport is Android-only), and taps, drags and ASCII text go through the same action bridge as `ui` (idb, or AXe); the Android key buttons have no iOS equivalent and are refused. The node bridge runs supervised, registered to the target's session |
| Network capture (HTTP/HTTPS) | ✅ | ⚠️ | mitmproxy, loopback-only, consent-gated; `network status` counts a flow as the target's only when its client is the target (`target_flows`) — the iOS Simulator shares the host's network stack, so its traffic and a host `curl` are indistinguishable (`host_traffic_indistinguishable`), and an Android device that cannot be read is `setting_unreadable`; `persistent_mocks_active` is raised by `network start`, `network status` and `doctor` |
| Response mocking | ✅ | ⚠️ | exact URL or glob + method/host; first enabled rule wins |
| Persistent mock registry | ✅ | ✅ | machine-level, survives restarts; full CRUD; reported by `doctor` |
| Process reaping | ✅ | ✅ | `processes` / `cleanup`, machine-wide; finds orphan proxies by signature when the registry is lost; the iOS log-stream writer, `canvas serve` pairs, and idb companions Autonom's own idb calls started are registered (`background`); every kill — `cleanup`, `session stop`, `record stop`, the log-stream restart — first checks the pid still runs the recorded command (`pid_reused`, `unverified_skipped`: whole arguments, never substrings; a companion row names its simulator's UDID; an empty signature verifies nothing), and a group whose recorded leader is gone is only reported (`group_remnants`, `group_remnant_left_running`), never signalled; registered command lines are redacted |
| HAR export | ✅ | ✅ | HAR 1.2, credentials redacted |
| Device proxy attach | ✅ | ⚠️ | emulator via `10.0.2.2`; iOS per-process env, not `URLSession`, reported as `mode: manual` / `attach_state: manual`. Flutter's `dart:io` `HttpClient` ignores the proxy environment and the Android global proxy alike: a Flutter app needs an in-app proxy hook (`flutter_proxy_hook_required`). Android `--install-ca` uses `adb root` and fails with `backend_failed` on a Play Store image |
| Credential redaction at capture | ✅ | ✅ | headers and body fields masked before writing |
| Flutter debug/test skills | ✅ | ⚠️ | iOS Flutter boundary partial |
| Native Android / Compose debug + perf skills | ✅ | — | |
| Native iOS project skills | — | ✅ | `ios-project-setup`, `ios-debugger-agent` |
| React Native skills | 🔜 | 🔜 | |
| Flow DSL: check / fmt / list | ✅ | ✅ | strict YAML subset, exact-match selectors by default, positioned errors; `platforms` is what a flow can really run on (`requires.platform`, else both minus iOS when a step there is Android-only); `docs/FLOW.md` |
| Flow DSL: run | ✅ | ✅ | polling assertions, single-fire mutations, `failure_class` + exit 1 for test failures, per-run `events.ndjson`; selectors match on-screen nodes only; a single file's result also lists its run under `runs`, like a suite's; a directory suite skips a flow its target cannot run (`flow_skipped_for_platform`); `waitForSettled` waits for a still screen; iOS `eraseText` dispatches HID backspace, proven on an iOS 26.5 simulator through AXe (2026-09-26) |
| Flow repair brief | ✅ | ✅ | a test failure in `flow run` carries `repair`: the `--until-step` prefix replay, a widened `ui find`, and the re-verification commands, plus advice keyed by the error code |
| Flow DSL: runFlow / tags / hooks execution | ✅ | ✅ | subflows inline with inherited appId, isolated `onFlowComplete`, `when:` conditions, tag-filtered directory suites, evidence policy |
| Flow DSL: Session → Flow compiler | ✅ | ✅ | `flow create --from-session` — proven selectors reused, secrets become `${SECRET_n}`, coordinate taps refuse to compile |
| PR Proof (local) | ✅ | ✅ | `proof --base` — covers-globs + pull-request tags select the suite; verdicts pass/fail/not_covered/blocked/inconclusive, never upgraded |
| Atlas-lite: observed screens/transitions graph | ✅ | ✅ | `atlas update|show|coverage|paths|export|diff`; fingerprints ride in run events; observed-only, unknown stays unknown |
| Evidence: step debugger + HTML/JUnit reports | ✅ | ✅ | manifest v3; addressable screenshots, hierarchy diff, logs, scrubbed requests; `report build|open|export|suite|serve`; loopback replay controls; JUnit for CI |
| Flow DSL: Maestro Core Profile import/export | ✅ | ✅ | `flow import`/`flow export --format maestro`; a timed `assertVisible`/`assertNotVisible` exports as `extendedWaitUntil`, a timed tap as `extendedWaitUntil` then the tap, `waitForSettled` as `waitForAnimationToEnd` (and back on import); outside-profile constructs refuse with `unsupported_flow_command` |
| Live session outputs catalog | ✅ | ✅ | `session outputs` — registered `streams[]` + directory scan, `abs_path`/`shell_hint` for `tail -f`; `metrics/` and `recordings/` artifacts are listed too (`followable: false` for binaries, `directory` for an Instruments `.trace`, with an `open`/`xdg-open` hint where the host has one) |
| Live follow (session files / device logs) | ✅ | ✅ | `logs follow` — NDJSON lines, always bounded by `--max-seconds`/`--max-lines`; the session's only device-log stream is the default; Android `--source device` starts at the device's now (`logcat -T`) unless `--from-start`, and `--package` narrows it the way `logs tail` does (the eof line names the `filter`); iOS `--source device` follows the installed app's bundle; `journal --follow` for the timeline |
| Network requests list | ✅ | ✅ | `network requests list --max N --since-id F` |
| Network requests follow | ✅ | ✅ | `network requests follow` — polls the store, emits only new flows as NDJSON |
| Metrics snapshot (memory/CPU summary) | ✅ | ✅ | `metrics snapshot` — Android meminfo/proc plus a fresh CPU sample (two `/proc` reads 0.5 s apart, percent of one core: a process busy on two cores reads 200), falling back to `dumpsys cpuinfo` with its window in `cpu_sampling` and a `cpu_stale` warning when that window is old; iOS **host** `ps`+container size; `metric_semantics` + `limitations` name the difference, never comparable 1:1 |
| Metrics series (directional growth) | ✅ | ✅ | `metrics series` — live capture or `--from-dir`; leads are directional only, never called a leak |
| Memory capture pack / HPROF | ✅ | ⚠️ | `metrics memory capture` (meminfo+proc+gfxinfo+HPROF, `--no-hprof`) + `analyze`; iOS gets `metrics memory warn` (best-effort stimulus) — full heaps go through `trace --preset allocations` |
| Performance traces (simpleperf / xctrace) | ✅ | ⚠️ | `metrics trace` — simpleperf records `--app` with `cpu-cycles` when `simpleperf list hw` lists it, else `cpu-clock` (emulators have no PMU); simpleperf/gfxinfo-flow proven against the fake adb; xctrace presets build correct argv and are fake-tested, unproven against a real Xcode recording in CI; `list-presets` marks `hitches` `unsupported_on_simulator` |
| Frame stats (gfxinfo / Flutter timings) | ✅ | ⚠️ | `metrics frames reset|capture` (best-effort parse, raw always kept) + `flutter-summary`; a window of 0 HWUI frames (Flutter draws outside HWUI) carries no percentiles and a `no_frames` warning. The iOS Simulator has no frame stats: Instruments refuses Animation Hitches there (`hitches` is physical-device only), so use `flutter-summary` on Flutter timings or `trace --preset time-profiler` |
| Flutter VM Service (widget tree, heap) | 🔜 | 🔜 | deliberately unshipped (Phase 4 D4) — profile-mode DevTools workflow stays documented in the Flutter skills |
| XCUITest execution | — | 🔜 | Separate from metrics |
| Optional MCP wrapper | 🔜 | 🔜 | CLI is source of truth first |

### `ui pinch|rotate|shake` have no backend

Neither platform can perform them. Android's `input` cannot express them, and
idb has no such command either — `idb ui` accepts only `describe-all`,
`describe-point`, `tap`, `button`, `text`, `key`, `key-sequence`, and `swipe`
(verified against fb-idb 1.1.7). Both platforms therefore refuse the three verbs
with `unsupported_on_platform` and a hint, rather than dispatching something the
tool will reject.

Until 0.15.1 the iOS path did dispatch them, and every real machine answered
with `backend_failed` wrapping idb's argparse usage text plus a hint pointing at
`doctor` — a tool that was working fine. The suite missed it because the fake
idb returned 0 for any argv: it proved *what was dispatched*, never *that the
dispatch names a command idb has*. The fake now carries idb's real command
surface and refuses anything outside it.

Use `ui swipe` for anything reachable by a drag. Rotation and shake need a hand
on the Simulator window (Device > Rotate).

## CLI surface

Every leaf command also accepts the target flags
`--platform android|ios`, `--target`, `--serial`, `--udid`, and the tool
overrides `--adb`, `--simctl`, `--idb`, `--idb-host`, `--idb-port`, before or
after the verb. Two iOS input overrides are global and go before the verb:
`--axe PATH` (the AXe binary, `AUTONOM_AXE`) and `--ios-hid auto|idb|axe`
(`AUTONOM_IOS_HID`).

```bash
autonom version
autonom devices [list] [--platform android|ios]
autonom devices boot [--avd NAME | --target ID] [--no-wait] [--timeout S] [--emulator PATH]
autonom devices shutdown [--target ID]
autonom doctor [--strict] [--mitmdump PATH]
autonom tour [--run] [--avd NAME] [--flow PATH] [--human] [--shutdown]
autonom capabilities

autonom session start [--app-id ID] [--install PATH] [--launch [ID]] [--activity C] [--log-stream]
autonom session show|stop
autonom session outputs [--session-id ID]
autonom session launch <app-id> [--activity C] [--arg A] [--setenv K=V] [--fresh]
autonom session force-stop|uninstall <app-id>
autonom session clear <app-id> [--strategy auto|reinstall|privacy]

autonom ui tree [--dump FILE] [--all] [--max-depth N] [--max-nodes N] [--format json|outline]
                [--interactable]
autonom ui wait --settled [--timeout-ms N] [--quiet-ms N]
autonom ui find [--text|--desc|--resource-id|--class-name|--package|--role] [--mode exact|contains|regex]
                [--case-sensitive] [--index N] [--clickable B] [--enabled B] [--all] [--dump FILE]
autonom ui tap [selector flags] | [--x X --y Y] [--duration MS]
autonom ui swipe --from X,Y --to X,Y [--duration S]
autonom ui pinch --at X,Y [--scale F] | ui rotate | ui shake   # refused on both platforms
autonom ui type <text> [--sensitive]
autonom ui key <keycode>

autonom flow check <path>
autonom flow fmt <path> [--write] [--check] [--diff] [--drop-comments]
autonom flow list [path]
autonom flow create --from-session <ID> [--out PATH] [--name N] [--task T]
autonom flow import <path> [--out PATH]
autonom flow export <path> [--format maestro] [--out PATH]
autonom flow run <path> [--include-tag TAG] [--exclude-tag TAG] [--env KEY=VALUE]
                 [--secret NAME] [--default-timeout-ms N] [--events] [--dry-run]
                 [--until-step N] [--evidence flow|minimal|on-failure|always]
                 [--collect screenshot|hierarchy|logs|crashes|network]

autonom teach start <name> | teach mark <name> | teach stop | teach show
autonom teach compile --out PATH [--recording ID] [--from MARK] [--to MARK]
autonom teach approve <flow> [--minimum-runs N] [--run] [--env KEY=VALUE] [--secret NAME]
autonom app-skill validate <app-id> [--workspace DIR]
autonom app-skill promote <app-id> <flow> [--approval FILE] [--workspace DIR]

autonom proof --base <REF> [--head REF] [--repo PATH] [--flows DIR] [--out DIR]
              [--env KEY=VALUE] [--secret NAME]

autonom atlas update [--session ID] [--app-id ID]
autonom atlas show [--app-id ID]
autonom atlas coverage [--app-id ID]
autonom atlas paths --from <SCREEN> --to <SCREEN> [--app-id ID]
autonom atlas export --out <PATH> [--app-id ID]
autonom atlas diff --base <PATH> [--head PATH] [--app-id ID]
autonom runtime-map update [--session ID] [--app-id ID]
autonom runtime-map show [--app-id ID]
autonom runtime-map coverage [--app-id ID]
autonom runtime-map paths --from <SCREEN> --to <SCREEN> [--app-id ID]
autonom runtime-map export --out <PATH> [--app-id ID]
autonom runtime-map diff --base <PATH> [--head PATH] [--app-id ID]

autonom report build [--session ID] [--run ID]
autonom report open [--session ID] [--run ID]
autonom report export [--session ID] [--run ID] [--format html|junit|allure|agent|csv|metrics] [--out PATH]
autonom report suite [--session ID] [--last N] [--out DIR] [--relative-to DIR] [--detailed] [--screenshots none|failed|all] [--open]
autonom report serve [--session ID] [--run ID] [--port N] [--open]
autonom report model [--session ID] [--run ID] [--out PATH]
autonom report bundle [--session ID] [--run ID] [--out DIR]
autonom report verify <bundle>
autonom report annotate <bundle> <text> [--author NAME] [--step ID]
autonom report gate [--session ID] [--run ID] [--rules FILE] [--out FILE]
autonom report history [--session ID] [--last N] [--out FILE]
autonom report watch [--session ID] [--interval-ms N] [--max-seconds N] [--max-runs N]
autonom report pack <bundle> --out FILE [--shard-id ID]
autonom report merge <packs...> --out DIR [--expected-shards N]
autonom report finalize <campaign> [--rules FILE] [--publish DEST]
autonom replay [--bundle DIR | --run ID] [--session ID] [--to-step STEP]

autonom ci run <path> --out DIR [--campaign-id ID] [--shard-id ID] [--expected-shards N]
               [--env KEY=VALUE] [--secret NAME] [--rules FILE] [--publish DEST]
autonom ci pack <bundle> --out FILE [--shard-id ID]
autonom ci merge <packs...> --out DIR [--expected-shards N]
autonom ci finalize <campaign> [--rules FILE] [--publish DEST]
autonom ci publish <campaign> <destination>
autonom agent export [--bundle DIR] [--session ID] [--run ID] --out FILE
autonom agent inspect [--bundle DIR] [--session ID] [--run ID] --step STEP

autonom screenshot [--label L] [--task T] [--out PATH]
autonom shots list [--task T] [--grep P] [--mocked-only] [--max N]
autonom shots show <path>
autonom record start [--name N] | record stop
autonom note add <text> [--task T] [--tag T] [--author A] | note list [--task --grep --max]
autonom journal [--kind action|note] [--verb V] [--task T] [--grep P] [--max N]
                [--follow] [--session-id ID] [--from-start] [--max-seconds N] [--max-lines N]

autonom metrics snapshot [--app-id ID] [--label L] [--task T] [--out PATH]
autonom metrics series [--app-id ID] [--label L] [--task T] [--out PATH] [--count N]
                       [--interval S] [--min-growth-kb N] [--from-dir DIR] [--glob G]
autonom metrics memory capture [--app-id ID] [--label L] [--out DIR] [--no-hprof]
autonom metrics memory analyze [--dir D] [--glob G] [--min-growth-kb N]
autonom metrics memory warn
autonom metrics frames reset [--app-id ID]
autonom metrics frames capture [--app-id ID] [--label L] [--out DIR]
autonom metrics frames flutter-summary <file> [--budget-ms F]
autonom metrics trace --preset <simpleperf|gfxinfo-flow|allocations|time-profiler|leaks|hitches>
                      [--duration S] [--app-id ID] [--label L] [--out DIR]
autonom metrics list-presets

autonom logs tail [--package ID] [--since S] [--max-lines N] [--grep P]
autonom logs follow [--source SRC | --path P] [--session-id ID] [--package ID] [--from-start]
                    [--max-seconds N] [--max-lines N] [--grep P] [--poll-ms N]
autonom crash list [--app-id ID] | crash show <name>
autonom open <url>
autonom permissions <grant|revoke|reset> <service> [app-id]
autonom location set <LAT,LON> | location get | location clear
autonom simulator battery <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator network <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator push <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator sms <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator call <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator biometric <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator clipboard <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator appearance <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator text-size <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator status-bar <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator keyboard <action> [--value KEY=VALUE] [--json OBJECT]
autonom simulator animations <action> [--value KEY=VALUE] [--json OBJECT]
autonom canvas serve [--port N] [--transport auto|screenrecord|screencap] [--fps N] [--token TOKEN] [--no-auth]
autonom media add <path>
autonom file ls [remote] [--app-id ID] | file pull <remote> [--app-id ID] [--out PATH]

autonom network start --i-understand-mitm [--port N] [--capture-bodies] [--mitmdump PATH]
                      [--ignore-hosts REGEX] [--intercept-connectivity-checks]
autonom network attach --i-understand-mitm [--install-ca] [--no-network-cycle]
autonom network detach|stop|status
autonom network requests list [--host --method --status --path --since --mocked --max --since-id]
autonom network requests follow [--host --method --status --path --mocked] [--interval S]
                                [--max N] [--max-seconds N] [--from-start]
autonom network requests show <id> [--full]
autonom network mock add [--url U | --match GLOB] [--method M] [--host H] [--status N]
                         [--header 'K: V'] [--json BODY | --body-file PATH] [--note N]
autonom network mock update <id> [--url U | --match GLOB] [--method M] [--host H] [--status N]
                                 [--header 'K: V'] [--json BODY | --body-file PATH] [--note N]
autonom network mock list [--all] | show <id> | remove <id> | clear
autonom network mock enable [<id>|--all] | disable [<id>|--all]
autonom network export [--har PATH]

autonom processes
autonom cleanup [--dry-run] [--all]
```

Every command prints JSON. Expected failures print
`{"ok": false, "error_code": "...", "error": "...", "hint": "..."}` on stderr with
exit code 2, so an agent can branch on `error_code` rather than parse prose.
`doctor` is the exception: it exits 0 even when tools are missing unless
`--strict` is passed, because a diagnostic that fails is useless in a pipeline.
A malformed regular expression is `invalid_value` and an output path that
cannot be written (`--out`, `--har`) is `output_not_writable`, both exit 2 and
both checked before the device is touched. A usage error's `hint` is the usage
of the verb that rejected the flag.

## Environment overrides

| Variable | Effect |
| --- | --- |
| `AUTONOM_HOME` | Overrides both state roots: sessions land in `$AUTONOM_HOME/sessions`, registries, the mitmproxy confdir, and `simulator-prefs/` snapshots directly beneath it |
| `XDG_STATE_HOME` | Machine state root when `AUTONOM_HOME` is unset (else `~/.local/state/autonom`) |
| `AUTONOM_ADB`, `AUTONOM_SIMCTL`, `AUTONOM_IDB`, `AUTONOM_EMULATOR`, `AUTONOM_MITMDUMP` | Binary paths, equivalent to the matching flag |
| `AUTONOM_IDB_COMPANION` | `host:port` of an idb companion on another Mac; every idb call gets `--companion host:port` (`--idb-host`/`--idb-port` set it) |
| `AUTONOM_AXE` | Path to the AXe binary used for iOS input when idb's HID cannot run (`--axe`) |
| `AUTONOM_IOS_HID` | iOS input backend: `auto` (default: idb, AXe when idb HID is known broken), `idb`, or `axe` (`--ios-hid`) |
| `AUTONOM_IOS_LOG_MAX_MB` | Per-file cap of the `session start --log-stream` file on iOS, in MB (default 50, one rotation) |
| `AUTONOM_IDB_STATE_FILE` | The fb-idb client's companion registry (default `/tmp/idb/state`), read to decide whether a stale-companion retry pruned anything |
| `AUTONOM_CORESIMULATOR_DEVICES` | The CoreSimulator `Devices` directory `simulator keyboard` edits (default `~/Library/Developer/CoreSimulator/Devices`); point it at a mounted tree to pin a remote Mac's simulator |
| `AUTONOM_PREFIX`, `AUTONOM_BIN_DIR` | Installer only: bundle home and the directory `autonom` is linked into |
| `AUTONOM_REQUIRE_SHELLCHECK` | Dev tooling only: `run_checks.sh` fails instead of skipping the shell lint when shellcheck is missing (set by CI) |

## Evidence ladder (unchanged)

code → unit/widget → integration on explicit target → screenshot + UI tree + logs →
profile/memory/network → before/after replay.

## Limitations closed

What each earlier limitation cost, and what replaced it.

| Earlier limitation | Autonom response |
| --- | --- |
| Kotlin/Compose-first scope | Six Flutter-specific skills plus hybrid routing |
| Codex-only packaging | Portable skills + one-command `install.sh` for Codex, Claude, Grok, generic agents |
| Static toolchain snapshot | Repository/local inspection with no “latest” assertion |
| Screenshot polling browser | H.264 + ffmpeg MJPEG path, persistent fallback, status and reconnect |
| Android-only device control | One verb set over Android and the iOS Simulator, with a shared compact node schema |
| Unauthenticated input bridge | Random token, localhost-only bind, allowlisted input, body limits |
| Exact text-only UI targeting | text/semantics/id/class/package, exact/contains/regex, waits and duplicate control |
| Directional memory capture only | Structured artifacts plus multi-capture trend analysis, while retaining proof rules |
| Limited executable validation | Python and Node tests, fake adb/simctl/idb backends, a recorded contract golden, a bare-host sweep, a TTY guard, and a doc-drift check — all run locally by `./scripts/run_checks.sh` |
| Generic Compose advice for Flutter apps | Flutter architecture, widgets, tests, performance, memory, platform, and release workflows |
| Project-local artifacts, invisible from elsewhere | Machine-global `~/.autonom/`: the session, its mocks, and orphaned processes are found and reaped from any directory |
| No record of what an agent did | Append-only `journal.ndjson` — every verb, its scrubbed argv, the result, and the failures, plus agent notes |
| A screenshot that silently showed mocked data | Provenance embedded in the PNG; captures taken under an active rule are flagged `screenshot_shows_mocked_data` |
| Before/after captures that differed by the battery glyph, the signal bars, or an autocorrected word | `simulator status-bar pin` and `simulator keyboard pin` fix the state the app does not own, so a diff shows only what the app changed; the clock stays real unless pinned on purpose |
| A failed flow that named the broken step and nothing else | The `repair` block: reconstruct the state, inspect the live tree, edit, re-verify — in the CLI's own commands |

The harness intentionally does not claim that trend analysis proves a memory
leak or that a browser preview proves performance. Those require retained-path
or repeatable runtime evidence.

## Competitive posture

| Class | Examples | Autonom stance |
| --- | --- | --- |
| Device MCP | mobile-mcp, Appium MCP, Maestro MCP | Interop later; Autonom wins on skills + evidence loop + multi-agent install |
| Code pattern skills | PromptSpace-style RN/Flutter/Swift packs | Expand domain skills; already strong on Flutter/Android |
| Network tools | mitmproxy, HTTP Toolkit, Proxyman | Wrap OSS (mitmproxy) behind `autonom network` |

See `docs/ARCHITECTURE.md` and `docs/plans/` for phased delivery (network, MCP).
