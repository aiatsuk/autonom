# Autonom capability matrix

Target: **universal mobile test/debug harness for AI agents** (Android + iOS).

Legend: ✅ shipped · ⚠️ partial · 🔜 planned · ❌ not planned for near term

| Capability | Android | iOS Simulator | Notes |
| --- | --- | --- | --- |
| Agent-portable skills (Codex / Claude / Grok) | ✅ | ✅ | install via marketplace or `install_skills.sh` |
| Unified device listing | ✅ | ✅ | `autonom devices`; each entry has a `running` flag |
| Boot / shut down a target | ✅ | ✅ | `devices boot --avd`/`--udid`, `devices shutdown`; refuses hardware. `devices boot --avd X --http-proxy HOST:PORT` (Android) routes ALL of the emulator's traffic through a proxy at launch — the launch-time flag transparent capture needs |
| Bootable AVD discovery | ✅ | — | `devices` reports an `avds` array on Android, plus `avd_profiles` (hardware profile, screen, density, API) and the `avd` a running emulator booted from |
| Explicit multi-target selection | ✅ | ✅ | `--platform` / `--target`; `--serial` and `--udid` are aliases |
| Guided first run | ✅ | ✅ | `autonom tour` — what the harness has, the workflow, this Mac's targets, and an offer to boot one and walk three screens into Settings with per-step screenshots, hierarchies and logs, an HTML report and a written account (`--run`, `--human`) |
| Environment diagnosis | ✅ | ✅ | `autonom doctor` — tools, capabilities (`ios_tree` and `ios_hid` separately: the tree needs idb, input needs a companion that loads SimulatorKit or AXe), orphans, every active `AUTONOM_*` override (`override_path_missing`), and emulators still routed through another session's live proxy (`device_attached_to_foreign_proxy`); `--strict` exits 1 with `ok: false` and the `strict_failures` list. `optional_tools.scrcpy` says where the Mobile Canvas would find scrcpy-server, from which `source`, at which `version`, and whether it is the 4.1 it needs (`ready`, `install_hint`); an optional tool never fails `--strict` |
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
| Logs | ⚠️ | ⚠️ | logcat; `log stream`/`log show`. Android `--package` narrows by the app's uid on API 31+ (plus the lifecycle lines naming it), else by its running pid, else to lines naming it, and says which in `filter` (`log_filter_degraded` when it is not the uid); an unknown package is `app_not_installed`. iOS asks `log` for the app's subsystem or executable (every Flutter app's executable is `Runner`) and keeps the records whose image UUID is the installed binary's or one the session recorded, so another `Runner` never leaks in and a reinstall — after which the log keeps naming the first container — hides nothing; it reports the `executable` and the `image_uuids` it matched on (`logs tail`, the follow's `eof` line). The iOS `--log-stream` file holds every same-named app's raw records — they count against its cap, and are not dropped when written, so a build installed while it runs is not lost — and every read narrows it: `logs tail --package` and `logs follow --source device --package` to that app, every other read of the file (`logs tail`, `logs follow` by default, `--source log_stream`, `--path logs/stream.ndjson` — `--path`/`--source` take no `--package`) to the session's app, by the identity its stream recorded (`log_stream_executable`, `log_stream_image_uuids`, `log_stream_bundle_path`; the simulator is asked, for at most 5 s, only when that install is gone, belongs to another app, or was never recorded, and never for a `--session-id` replay); raw when nothing identifies the app; any other `--path` is read raw. The file is capped on disk (`AUTONOM_IOS_LOG_MAX_MB`, one rotation), registered to the session, and started again after a `simulator keyboard pin` reboot or a `session clear --strategy reinstall` (`log_stream_restarted`, or a `log_stream_stopped` warning) |
| Crash reports | ⚠️ | ✅ | Android: crash logcat buffer; iOS: idb crash store, and a listing that failed (idb lost its companion) is an error, never an empty list |
| Deep links | ✅ | ✅ | `autonom open <url>`; Android sends the VIEW intent with `am start -W` and reports `handled_by`, the activity that took the URL (`url_opened_chooser` when it is the system chooser, `url_not_handled` when nothing took it); iOS `simctl openurl` cannot say, so `handled_by: "unknown"` |
| Permissions | ✅ | ✅ | `pm grant/revoke/reset`; `simctl privacy`, the service checked against what this Xcode's `simctl help privacy` lists |
| Simulated location | ⚠️ | ✅ | set: iOS simctl / Android emulator `geo fix`; `location get` reads the system's last fix on Android and reports `requested` + `delivered` against what the session set (iOS has no read-back) |
| Media library seeding | ✅ | ✅ | `autonom media add` |
| App-container file access | ✅ | ✅ | `autonom file ls\|pull`, confined to the container; a release/system app refuses with `app_not_debuggable` |
| Remote target host | — | ✅ | idb client can drive a companion on another Mac: `--idb-host/--idb-port` or `AUTONOM_IDB_COMPANION` add `--companion host:port` to every idb call |
| Emulator browser mirror | ✅ | ⚠️ | `canvas serve` (`android-emulator-browser` skill). Android `--transport auto` prefers **scrcpy** when a scrcpy-server 4.1 is found (`--scrcpy-server`, `AUTONOM_SCRCPY_SERVER`, `SCRCPY_SERVER_PATH`, then an installed `scrcpy`; never downloaded, any other version refused): one server per Canvas sends the device's own H.264 untranscoded over `/ws/video` to a WebCodecs page (the multipart picture, with the reason, when the browser cannot decode it), and touch (10 pointers, held fingers, Ctrl/Alt pinch), wheel scroll, key down/up with meta state, Unicode text, paste, clipboard fetch and system buttons go over an authenticated `/ws/control` as they happen. Unicode text and paste replace the device clipboard. One journal record per completed action (`transport: scrcpy`). Otherwise `screenrecord` + ffmpeg MJPEG (device H.264 probed by running it, since API 36 hides the option from `--help`), else screencap; `/status` `fallback_reason` says why, and all multipart pages share one capture loop. iOS: `--transport auto` prefers **idb** when idb_companion is found (`--idb-companion`, `AUTONOM_IDB_COMPANION_BIN`, then `PATH`; never `AUTONOM_IDB_COMPANION`, the remote companion of idb calls; `--transport idb` refuses to start without it, `tool_missing` with `canvas.idb`): the Canvas starts its own companion on a private socket and sends one H.264 stream (60 fps, full resolution, about 12 Mbit/s, a key frame every 2 s) to the same WebCodecs page over `/ws/video`, shared by every tab, always ended with an explicit Stop, restarted with a new companion after an abnormal end (including a Stop/Start whose Stop was not written or answered, before any new Start) and with Stop/Start when the Simulator is Booted again; the page turns the portrait picture for the reported orientation. Over `/ws/control` one finger goes live over the companion's HID stream (a second pointer is refused), the wheel is a short synthetic drag, Home and Power press Home and Lock, and text goes through the action bridge; one journal record per completed action (`transport: idb`). Otherwise frames are polled `simctl io screenshot` PNGs (the `scrcpy` and `screenrecord` transports are Android-only), and taps, drags and ASCII text go through the same action bridge as `ui` (idb, or AXe); the page's Home and Power press the Simulator's Home and Lock buttons, and the other Android key buttons have no iOS equivalent, are hidden and are refused. Android display presets (`small` 720x1280 @ 320, `pixel-11` 1080x2424 @ 420, `pixel-fold` 2208x1840 @ 420, `tablet` 2560x1600 @ 320, `default` the device's own) change `wm size` and `wm density` on the Canvas serial while everything keeps running, from the page's Size menu, `{"t":"display"}` on `/ws/control` or `POST /display`, under the same handoff rules as input; `/status` `display` reports the preset, size and density, each change is one `ui display` journal record, and the Canvas puts the original values back when it stops (not after SIGKILL). The node bridge runs supervised, registered to the target's session |
| Network capture (HTTP/HTTPS) | ✅ | ⚠️ | mitmproxy, loopback-only, consent-gated; `network status` counts a flow as the target's only when its client is the target (`target_flows`) — the iOS Simulator shares the host's network stack, so its traffic and a host `curl` are indistinguishable (`host_traffic_indistinguishable`), and an Android device that cannot be read is `setting_unreadable`; `persistent_mocks_active` is raised by `network start`, `network status` and `doctor`. `network status` reports `capture_mode` (`transparent`/`app_proxy`) and, on the transparent path, the installed `system_ca` |
| Transparent Android capture (no app change) | ✅ | — | `network attach --system-ca` on a rooted `google_apis` emulator booted with `devices boot --http-proxy` to the session proxy: installs the MITM CA into the SYSTEM trust store (reversible tmpfs + zygote mount-namespace bind) and verifies it from a zygote namespace (`system_ca.verified`), so even Flutter `dart:io` and pinned-store traffic are captured with zero app modification. **Verified live on API ≥ 34** (APEX conscrypt); the API < 34 `/system/etc/security/cacerts` remount is implemented but not yet device-verified. In this mode every flow on the proxy is counted as the device's (they arrive from `127.0.0.1` by design; a host process using the port would be counted too, which is acceptable on a dedicated test host), so `network status` answers `attached: true`, `evidence: transparent_proxy`, `unattributed_flow_count: 0` — but only while the process registry still shows the emulator booted routed through the proxy; after a shutdown or an unrouted reboot it answers `attached: false`, `evidence: transparent_route_gone`. `network requests` lists all flows. `--system-ca` is refused (`reason: app_proxy_attached`) while an app-proxy attach is active; `network detach` first. A flow's `host` is the requested name (Host header), not the CONNECT IP (kept as `server_ip`), so `--host` filters and `--host` mocks match. Refuses (`unsupported_capability`, `network.system_ca`) on a non-rootable / Play image, and (`network.transparent_capture`) when the emulator was not booted proxy-routed. The device-proxy + user-CA path stays as a fallback with the honest warning that Flutter/pinned traffic is not captured that way |
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
| Live follow (session files / device logs) | ✅ | ✅ | `logs follow` — NDJSON lines, always bounded by `--max-seconds`/`--max-lines`; the session's only device-log stream is the default; Android `--source device` starts at the device's now (`logcat -T`) unless `--from-start`, and `--package` narrows it the way `logs tail` does (the eof line names the `filter`); iOS `--source device` follows the session's stream file while its writer runs, else a live `log stream`, or a past session's file with `--session-id` — with `--package` only that app's records, matched by binary UUID, and without one the session's file is read as the session's app (a live stream without `--package` is the whole device log); `logs follow` of the session's own stream file (the default, `--source log_stream`, `--path logs/stream.ndjson[.1]`, or a hard link to it; these take no `--package`) is the session's app too — raw when nothing identifies the app — any other `--path` is raw, and the eof line names the `image_uuids`; `journal --follow` for the timeline |
| Network requests list | ✅ | ✅ | `network requests list --max N --since-id F` |
| Network requests follow | ✅ | ✅ | `network requests follow` — polls the store, emits only new flows as NDJSON |
| Metrics snapshot (memory/CPU summary) | ✅ | ✅ | `metrics snapshot` — Android meminfo/proc plus a fresh CPU sample (two `/proc` reads 0.5 s apart, percent of one core: a process busy on two cores reads 200), falling back to `dumpsys cpuinfo` with its window in `cpu_sampling` and a `cpu_stale` warning when that window is old; iOS **host** `ps`+container size; `metric_semantics` + `limitations` name the difference, never comparable 1:1 |
| Metrics series (directional growth) | ✅ | ✅ | `metrics series` — live capture or `--from-dir`; leads are directional only, never called a leak |
| Memory capture pack / HPROF | ✅ | ⚠️ | `metrics memory capture` (meminfo+proc+gfxinfo+HPROF, `--no-hprof`) + `analyze`; iOS gets `metrics memory warn` (best-effort stimulus) — full heaps go through `trace --preset allocations` |
| Performance traces (simpleperf / xctrace) | ✅ | ⚠️ | `metrics trace` — simpleperf records `--app` with `cpu-cycles` when `simpleperf list hw` lists it, else `cpu-clock` (emulators have no PMU); simpleperf/gfxinfo-flow proven against the fake adb; xctrace presets build correct argv and are fake-tested, unproven against a real Xcode recording in CI; `list-presets` marks `hitches` `unsupported_on_simulator` |
| Frame stats (gfxinfo / Flutter timings) | ✅ | ⚠️ | `metrics frames reset|capture` (best-effort parse, raw always kept) + `flutter-summary`; a window of 0 HWUI frames (Flutter draws outside HWUI) carries no percentiles and a `no_frames` warning. The iOS Simulator has no frame stats: Instruments refuses Animation Hitches there (`hitches` is physical-device only), so use `flutter-summary` on Flutter timings or `trace --preset time-profiler` |
| Flutter VM Service (widget tree, heap) | 🔜 | 🔜 | deliberately unshipped (Phase 4 D4) — profile-mode DevTools workflow stays documented in the Flutter skills |
| XCUITest UI backend | — | ✅ | optional bundled runner (`native/ios`) behind `--ui-backend auto|idb|xcuitest`: `auto` keeps idb and falls back for an app session whose idb tree is empty, has no usable root frame or fails, and records `fallback_reason`; tree, find, selector and coordinate tap, long press, swipe, text and Home; single dispatch (`ui_action_uncertain`, never repeated or re-sent); stopped by `session stop`; Simulator only, builds on first use; see `docs/IOS_UI_RECOVERY.md`. Running an app's own XCUITest suites is not a goal |
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

### Mobile Canvas transports

| Transport | Picture | Input | Needs |
| --- | --- | --- | --- |
| `scrcpy` (Android) | device H.264 over `/ws/video`, decoded by WebCodecs in the page and shown in order (at most two frames wait, one per screen refresh); up to 60 fps unless `--fps` caps it, about 59 presented fps on a `-gpu host` API 36 emulator at the default size; `--max-size` is the longer side; `--bit-rate` defaults to 12 Mbit/s | `/ws/control`, as it happens: touch with hold and up to 10 pointers, scroll, key down/up with meta state, Unicode text and paste through the device clipboard, clipboard fetch, Back/Home/Apps/Power/Volume/Wake/Notifications/Quick settings/Collapse/Rotate, display presets (`{"t":"display"}`; the video follows the new size without a restart) | scrcpy-server 4.1; a browser with an H.264 `VideoDecoder` for the decoded picture, else the multipart picture |
| `screenrecord` (Android) | device H.264 → ffmpeg → MJPEG at `--fps` (default 15); `--max-size` is the width | HTTP: tap, swipe, allowlisted keys, conservative ASCII text, display presets (`POST /display`; the capture restarts at the new size) | ffmpeg; a device whose `screenrecord --output-format=h264` produces H.264 |
| `screencap` (Android, iOS) | multipart screenshots, at most 10 fps | HTTP as above (display presets on Android only); iOS through idb or AXe, without the Android key buttons or display presets | nothing extra |

`auto` takes the first available in that order on Android and reports
`fallback_reason`; iOS always uses screenshots. On every transport,
pause/resume/takeover/release govern all input and every action is journaled
with its origin. The scrcpy path journals one record per completed gesture,
wheel burst, key press, text entry, paste, system button and handoff change,
never clipboard or sensitive text; `/status` `scrcpy.journal_pending` and
`scrcpy.journal_dropped` count the records waiting for the bridge and those
dropped because 256 were already waiting. A display preset change is one
`ui display` record on every Android transport, and the restore at stop one
more with origin `system`.

## CLI surface

Device command leaves also accept the target flags
`--session-id <id>`, `--ui-backend auto|idb|xcuitest`,
`--platform android|ios`, `--target`, `--serial`, `--udid`, and the tool
overrides `--adb`, `--simctl`, `--idb`, `--idb-host`, `--idb-port`, before or
after the verb. Two iOS input overrides are global and go before the verb:
`--axe PATH` (the AXe binary, `AUTONOM_AXE`) and `--ios-hid auto|idb|axe`
(`AUTONOM_IOS_HID`). `--ui-backend` chooses who serves the iOS tree and
input (`AUTONOM_UI_BACKEND`, or the choice `session start --ui-backend`
stored); `--ios-hid` only routes HID input while idb serves the tree. For
artifact commands such as reports, put session selection before the verb:
`autonom --session-id <id> report build`.

```bash
autonom version
autonom devices [list] [--platform android|ios]
autonom devices boot [--avd NAME | --target ID] [--no-wait] [--timeout S] [--emulator PATH]
                     [--http-proxy HOST:PORT]
autonom devices shutdown [--target ID]
autonom doctor [--strict] [--mitmdump PATH]
autonom tour [--run] [--avd NAME] [--flow PATH] [--human] [--shutdown]
autonom capabilities [--probe]

autonom session start [--app-id ID] [--install PATH] [--launch [ID]] [--activity C] [--log-stream]
autonom session show|stop
autonom session outputs [--session-id ID]
autonom session launch <app-id> [--activity C] [--arg A] [--setenv K=V] [--fresh]
autonom session force-stop|uninstall <app-id>
autonom session clear <app-id> [--strategy auto|reinstall|privacy]

autonom ui tree [--dump FILE] [--all] [--max-depth N] [--max-nodes N] [--format json|outline]
                [--interactable] [--no-accessibility-recovery]
autonom ui accessibility <status|enable|reset>   # Android emulator session
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
autonom canvas serve [--port N] [--transport auto|scrcpy|idb|screenrecord|screencap] [--fps N]
                     [--max-size PX] [--bit-rate BPS] [--scrcpy-server PATH [--scrcpy-version X.Y]]
                     [--idb-companion PATH] [--token TOKEN] [--no-auth]
autonom media add <path>
autonom file ls [remote] [--app-id ID] | file pull <remote> [--app-id ID] [--out PATH]

autonom network start --i-understand-mitm [--port N] [--capture-bodies] [--mitmdump PATH]
                      [--ignore-hosts REGEX] [--intercept-connectivity-checks]
autonom network attach --i-understand-mitm [--install-ca] [--no-network-cycle] [--system-ca]
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
| `AUTONOM_IDB_COMPANION` | `host:port` of an idb companion on another Mac; every idb call gets `--companion host:port` (`--idb-host`/`--idb-port` set it). Never a binary path: the Mobile Canvas does not read it |
| `AUTONOM_IDB_COMPANION_BIN` | The idb_companion binary the Mobile Canvas idb transport starts, read after `canvas serve --idb-companion` and before `idb_companion` on `PATH`; a set value that is not an executable file is reported, not skipped |
| `AUTONOM_AXE` | Path to the AXe binary used for iOS input when idb's HID cannot run (`--axe`) |
| `AUTONOM_IOS_HID` | iOS input backend: `auto` (default: idb, AXe when idb HID is known broken), `idb`, or `axe` (`--ios-hid`) |
| `AUTONOM_SCRCPY_SERVER`, `SCRCPY_SERVER_PATH` | The scrcpy-server file of the Mobile Canvas scrcpy transport, read in this order after `canvas serve --scrcpy-server`; the version comes from the file name (`scrcpy-server-v4.1`), and a set variable that points at a missing or other-version file is reported, not skipped |
| `AUTONOM_IOS_LOG_MAX_MB` | Per-file cap of the `session start --log-stream` file on iOS, in MB (default 50, one rotation) |
| `AUTONOM_IDB_STATE_FILE` | The fb-idb client's companion registry (default `/tmp/idb/state`), read to decide whether a stale-companion retry pruned anything |
| `AUTONOM_CORESIMULATOR_DEVICES` | The CoreSimulator `Devices` directory `simulator keyboard` edits (default `~/Library/Developer/CoreSimulator/Devices`); point it at a mounted tree to pin a remote Mac's simulator |
| `AUTONOM_PREFIX`, `AUTONOM_BIN_DIR` | Installer only: bundle home and the directory `autonom` is linked into |
| `AUTONOM_REQUIRE_SHELLCHECK` | Dev tooling only: `run_checks.sh` fails instead of skipping the shell lint when shellcheck is missing (set by CI) |
| `AUTONOM_TEST_JOBS` | Dev tooling only: worker count of `tests/run_parallel.py`, the parallel unit-suite runner `run_checks.sh` uses (default: twice the CPU count) |

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
| Screenshot polling browser | scrcpy H.264 decoded in the page with real-time touch, keys and clipboard; H.264 + ffmpeg MJPEG and screenshots as shared fallbacks with a stated reason; status and reconnect |
| Android-only device control | One verb set over Android and the iOS Simulator, with a shared compact node schema |
| Unauthenticated input bridge | Random token, localhost-only bind, Host check, cookie + CSRF + Origin for WebSockets, an unframeable page, allowlisted and strictly validated input, body and message limits |
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
