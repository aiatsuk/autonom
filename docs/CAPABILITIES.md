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
| Emulator browser mirror | ✅ | ⚠️ | `canvas serve` (`android-emulator-browser` skill). Android `--transport auto` prefers **scrcpy** when a scrcpy-server 4.1 is found (`--scrcpy-server`, `AUTONOM_SCRCPY_SERVER`, `SCRCPY_SERVER_PATH`, then an installed `scrcpy`; never downloaded, any other version refused): one server per Canvas sends the device's own H.264 untranscoded over `/ws/video` to a WebCodecs page (the multipart picture, with the reason, when the browser cannot decode it), and touch (10 pointers, held fingers, Ctrl/Alt pinch), wheel scroll, key down/up with meta state, Unicode text, paste, clipboard fetch and system buttons go over an authenticated `/ws/control` as they happen. Unicode text and paste replace the device clipboard. One journal record per completed action (`transport: scrcpy`). Otherwise `screenrecord` + ffmpeg MJPEG (device H.264 probed by running it, since API 36 hides the option from `--help`), else screencap; `/status` `fallback_reason` says why, and all multipart pages share one capture loop. iOS: `--transport auto` prefers **idb** when idb_companion is found (`--idb-companion`, `AUTONOM_IDB_COMPANION_BIN`, then `PATH`; never `AUTONOM_IDB_COMPANION`, the remote companion of idb calls; `--transport idb` refuses to start without it, `tool_missing` with `canvas.idb`): the Canvas starts its own companion on a private socket and sends one H.264 stream (60 fps, full resolution, about 12 Mbit/s, a key frame every 2 s) to the same WebCodecs page over `/ws/video`, shared by every tab, always ended with an explicit Stop, restarted with a new companion after an abnormal end (including a Stop/Start whose Stop was not written or answered, before any new Start) and with Stop/Start when the Simulator is Booted again; the page turns the portrait picture for the reported orientation. Over `/ws/control` one finger goes live over the companion's HID stream (another connection's pointer or a third one is refused); a second pointer of the same connection, or the page's Ctrl/Alt-drag, is one pinch sent at release as idb's canned HIDPinch (center, radius, scale, duration capped at 2 s; no live motion, no two-finger pan or rotate, since idb has no second live finger); the wheel is a short synthetic drag; Home, Power, Volume up and Volume down press the Simulator's Home, Lock and volume buttons (the Mac's volume is untouched); text goes through the action bridge; on the Simulator's own Mac `clipboard-get` reads the Simulator clipboard (`xcrun simctl pbpaste`) and `paste` sets it (`xcrun simctl pbcopy`) and types it when it is plain ASCII of at most 300 bytes (other text is only set: long-press Paste inserts it), never logging the text, while a Canvas not on macOS refuses both; one journal record per completed action (`transport: idb`). Otherwise frames are polled `simctl io screenshot` PNGs (the `scrcpy` and `screenrecord` transports are Android-only), and taps, drags and ASCII text go through the same action bridge as `ui` (idb, or AXe); the page's Home and Power press the Simulator's Home and Lock buttons, and the other Android key buttons have no iOS equivalent, are hidden and are refused. Android display presets (`small` 720x1280 @ 320, `pixel-11` 1080x2424 @ 420, `pixel-fold` 2208x1840 @ 420, `tablet` 2560x1600 @ 320, `default` the device's own) change `wm size` and `wm density` on the Canvas serial while everything keeps running, from the page's Size menu, `{"t":"display"}` on `/ws/control` or `POST /display`, under the same handoff rules as input; `/status` `display` reports the preset, size and density, each change is one `ui display` journal record, and the Canvas puts the original values back when it stops (not after SIGKILL). The node bridge runs supervised, registered to the target's session |
| Mobile Canvas web controls (Unreleased) | ✅ | ⚠️ | The Canvas page's Tools drawer (header Tools toggle; it takes the inspector's column, a full-width sheet at phone width) works on the Canvas's own target through one tools process (`scripts/autonom_canvas_tools.py`, NDJSON, apart from the input bridge) and the CLI's own `autonom_lib` functions. **App**: app id from the session on this target or the best-known app; Android lists the app's requested runtime permissions with their granted state and grants, revokes or resets one; iOS grants, revokes or resets a privacy service from this Xcode's `simctl privacy` list (Xcode 27 has no `camera`; an unlisted service is 400 `invalid_value`) but cannot read the state back; location by latitude/longitude or a preset, Android read-back (delivered or requested only), Clear on iOS only (the emulator has no reset), no iOS read-back. **Simulate**: push (iOS, at most 4 KB; the Simulator refuses pushes to some system apps, Safari among them, which is 502 `backend_failed`), biometrics, battery, network online/offline (Android), appearance; emulators and Simulators only. **Network**: status, start after a confirmation naming the MITM effect (never `--capture-bodies`), attach, detach, stop, a live request list polled with `since_id` while visible, filters, redacted previews only, Mock this; needs an Autonom session on this target and never starts one. **Mocks**: list with hit counts (only this target's session), add, edit, enable, disable, remove, clear after a confirmation; machine-wide (every session on this Mac). **Logs**: `logs follow --source device` child per Canvas, starting at the device's current time once that child runs (a line written in its first second or so may be missing), ring of 2000 lines / 1 MiB with `seq` and `dropped` (a Simulator logging thousands of lines a second overruns it; the lost lines are counted in `dropped`, and an agent can pass `match` to keep only lines holding a text at the source), level and text filters, pause, clear; stops 60 s after the last read, on close and at Canvas stop; iOS only when the session on this Simulator was started with `--log-stream`. Routes `POST /tools/call`, `GET`/`POST /tools/logs` behind the Canvas authorization and CSRF; device changes follow the handoff rule (403 `control_refused`), 60 s timeout (504), `tools_unavailable` (502) restarts once per 10 s; journaled as `canvas <op>` with the origin, never mock bodies, push payloads or log text; `/status` `tools` |
| Canvas workspace, several devices (Unreleased) | ✅ | ⚠️ | `autonom canvas` (or `canvas serve` with no target) starts a workspace Canvas with no device and no session; it runs until Ctrl+C or `canvas stop`, and detaching the last device never stops it. In-page tabs (at most 8), each a split of 1-4 tiles with its own URL `/c/<tab id>`; a device lives in one tab (others show "In tab <name>" with "Move here"); at most 8 devices per Canvas. Attaching starts an Autonom session for that device (`--alongside`, never touching `current.json`) or reuses the live one on that target; detaching ends only sessions the Canvas started. The focused tile streams at full rate, the others at most 30 fps and 1024 px (Android) or half scale (iOS); tabs no window shows stop streaming. A crash in one device restores that device only. Tabs and the devices attached at stop persist per workspace (`--workspace NAME`, `--ephemeral`); `--bootable` lets the picker boot an AVD or Simulator, `--shutdown-booted` shuts down what the Canvas booted. An Actions drawer per device: screenshot, recording (180 s), install from `--install-root` folders only, launch, open link, app language, a captures gallery in `~/Downloads/Autonom/<device>/` (200 files / 2 GB, oldest deleted), the command log and tool health. CLI: `canvas stop`, `canvas attach`, `canvas detach`, `canvas list`. iOS needs macOS |
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
| Ticket pre-test: record, context, baseline | ✅ | ✅ | `pretest start --ticket FILE\|- --base REF` — one active pre-test per session (`pretest_already_active`); the ticket is masked before it is written; `context.json` holds changed files with hunk ranges, renames, binaries, the `proof` flow selection and Runtime Map hints (`hint: true`, never coverage) — hunk `functions` only for code files (not Markdown, text, ARB, JSON, YAML and other data files) and never an import/package line; `baseline.json` records crashes, log cursor, captured-request count and one metrics snapshot, each `unavailable` with a reason when it cannot be read; `pretest status\|context\|stop\|list` |
| Ticket pre-test: attribution | ✅ | ✅ | while a check is open every journal entry, note, screenshot, saved `ui tree` and action detail carries `pretest_id` and `check_id`, and the check id is the `task` when none was given; network flows and log lines are attributed by the check's time window at pack time; after each `network mock` change (and at `pretest start`) the mock registry is journaled as a `mock_state` entry (ids, URL globs, methods, hosts, statuses, enabled flags; never headers or bodies), so each step lists the mocks active at its time; without a pre-test nothing gains a key or entry |
| Ticket pre-test: budgets and forbidden actions | ✅ | ⚠️ | `--max-actions` (300; a whole number ≥ 1) and `--max-minutes` (60; a finite number > 0 — NaN, inf, 0 and negatives are `invalid_value`) count `ui tap\|long-press\|double-tap\|swipe\|type\|key\|gesture`, `open`, app launch/stop/clear and each flow input step (`budget_exhausted`); a tap on text matching `--forbid-text` (default `buy\|pay\|purchase\|delete\|remove account\|купить\|оплатить\|удалить`) or at a point whose tree cannot be read is `forbidden_action`; an app outside `--allow-app` is `app_not_allowed` (an Android link no app has verified opens the system app chooser and is refused as such, `chooser: true`) — all before any input reaches the device. Every guarded point tap reads a fresh tree (no reuse between taps), so rapid repeat taps (a flow `tapOn` with `repeat`, a double tap by points) are slow during a pre-test — about one tree read (~2.5 s) per tap — and a race criterion passes only with an observed request count, else `inconclusive`. iOS `open` is counted but not checked against the allow list (iOS cannot say which app takes a URL); a flow `eraseText` counts each key press; the `ui tree` accessibility relaunch is exempt |
| Ticket pre-test: checks and findings | ✅ | ✅ | `pretest check add\|begin\|end\|list` — one open check at a time, verdicts pass/fail/blocked/not_covered/inconclusive; `pretest finding add` with evidence refs (`shot:`, `tree:`, `action:`, `request:`, `log:`, `run:`, `crash:`) resolved at write time (`evidence_ref_unknown`); `pretest flows` runs the selected flows as `regression` checks (passed→pass, failed→fail, broken→blocked, all skipped→inconclusive) and prints a `flows` list of `{flow, check_id, run_id, status, verdict}`; `check end` of an ended check is always `no_open_check` (its verdict and note never change) |
| Ticket pre-test: device health | ✅ | ✅ | `pretest health` (and `pack`) — new crashes, grouped app error logs (logcat E/F/A or iOS error/fault lines, plus app lines of any level whose message is a JSON or `key=value` record with `level` error/fatal/severe, and Flutter `FLUTTER_ERROR` / `══╡ EXCEPTION CAUGHT` markers), 5xx / unmocked 4xx / unfinished requests and metrics leads since the baseline, each an automatic finding (`origin: auto`, `severity: info`, `confirmed: false`) attributed to the check whose window holds it; automatic findings never change a verdict |
| Ticket pre-test: pack and verify | ✅ | ✅ | `pretest pack [--out DIR]` — ends the pre-test (an open check becomes `inconclusive`), computes the ticket verdict, and writes `summary.json`, paste-ready `summary.md`, an offline `index.html`, content-addressed evidence and nested flow Bundles v2 through one atomic rename; re-packing is idempotent; screenshots taken while mocks were active are marked (`mocked: true`, `mocks_active` in `summary.json`, "(mocked data)" in `summary.md`, a "Mocked" tag in `index.html`); each check lists the requests captured in its window (method, host, path without the query, status, mocked, duration; at most 200 with `requests_truncated`; never headers or bodies) in `checks/<id>.json`, `summary.json`, `summary.md` and `index.html`; a finding's steps run from its check start to its latest evidence; `summary.md` gives each not covered, blocked or open check with its note; in `index.html` opened from disk the review commands carry the pack's real directory (shell-quoted; `<pack>` without JavaScript) and the verdict reason names the check title; `report verify <pack>` checks every digest including nested bundles; a packed pre-test refuses changes (`pretest_finalized`) |
| Ticket pre-test: QA review and kit | ✅ | ✅ | `pretest review <pack> approve\|more-checks\|return-to-dev` writes an annotation outside the integrity set (`report verify` stays ok); `return-to-dev` needs `--finding` (`review_needs_finding`), `--confirm`/`--dismiss` take automatic findings only (`not_automatic`); `more-checks --check TITLE` starts a linked child pre-test with the parent's ticket and diff context, but with the reviewer's own rules: `--app-id`, `--max-actions`, `--max-minutes`, `--forbid-text` and `--allow-app` on the review, else this session's app and the defaults, never the pack's (the child's `app_id` and `budget` are in the output); `pretest review-status <pack>`; `pretest kit` lists the 18 edge cases and, with an active pre-test, their coverage |
| Ticket pre-test: regression drafts, repro and rerun | ✅ | ✅ | `pretest flow-draft <finding-id> [--pack PATH] [--out PATH]` compiles the journal range of the finding's check (Session→Flow compiler) into `<repo>/.autonom/flows/drafts/<ticket-slug>-<finding-id>.yaml` with the `draft` tag, `properties.covers` set to the changed files and a closing assertion from that check's own last `ui find`; the file must pass `flow check`, is never overwritten with other content, and its path is recorded on the finding (with `--pack` nothing is recorded); `pretest repro <finding-id> --times N` (1–10) replays the draft and records `reproduced: k/N`, `stability` (`stable`/`intermittent`/`not_reproduced`) and the run ids — N is always the `--times` value: a failed run (test failure) reproduces the finding, a passed run does not, and the first run with no verdict (broken, skipped, aborted or refused by a guard) stops repro, records nothing and returns its cause; the finding always stays; on a pack it is refused (`pretest_finalized`); `pretest rerun <pack> [--check ID]...` starts a linked child pre-test (`kind: rerun`) with this session's rules, runs each chosen check's flow or draft, and the child's pack shows `before`/`after` per check while the parent pack is only read |
| Ticket pre-test: app profile | ✅ | ✅ | one read order: the workspace's `.autonom/apps/<app-id>/profile.yaml`, else `$AUTONOM_HOME/apps/<app-id>/profile.yaml` (`~/.autonom` by default); `pretest context` reports the profile used (`profile.path`, `profile.source`) or `null`; keys `login_flow`, `warmup_flow`, `accounts` (names → environment variable names only, each starting with `AUTONOM_` or `PRETEST_`; warm-up reads no other variable), `fixtures`, `flaky_screens`, `events`; parsed with the flow files' YAML subset; a credential-shaped value or a secret-named field holding a value refuses the profile (`profile_invalid`, naming the key path, never the value), and `pretest start` then creates nothing (schema `schemas/pretest-profile-v1.schema.json`) |
| Ticket pre-test: warm-up | ✅ | ✅ | `pretest warmup` runs the profile's `login_flow`, then `warmup_flow` (the account variables that are set go to the runs as secrets), updates the Runtime Map from the session like `atlas update`, and reports each Runtime Map screen hint `reached` (replaying recorded selector-tap transitions along the shortest known path), `unreplayable` (a path holds a swipe, key, coordinate tap or relational selector), `known` (in the map, not reached, with a `reason`) or `unknown` (not in the map); no exploration beyond recorded transitions; flow steps and replayed taps count against the budget; refused while a check is open (`check_already_open`) |
| Ticket pre-test: accessibility check | ✅ | ✅ | `pretest a11y [--check ID]` reads the current tree once, saves it under `trees/`, and writes an automatic finding (`source: a11y`, `severity: minor`, `confirmed: false`, bounds in the text and `data`, the saved tree as `tree:` evidence) per unlabelled control, control smaller than 48×48 dp with a 0.5 dp rounding tolerance (Android, density from `wm density` or `ro.sf.lcd_density`; skipped with `density_unknown` when neither answers) or 44×44 pt (iOS), and group of controls sharing one label; once per check, screen and problem; read-only (never counted) |
| Ticket pre-test: analytics event check | ✅ | ✅ | `pretest events [--check ID]` matches the profile's `events` rules (method, host or host glob, path glob without the query, optional `body_key` — dotted for nested JSON, or a form field — and `body_value`) against the captured requests in the check's window (the open check's, else the whole pre-test's) and reports `matched` (with request ids), `missing` and `unexpected` (requests to a rule's endpoint that match no rule, with body key names only); each missing rule is an automatic finding (`source: analytics`, `severity: minor`); body values are never copied anywhere; without captured traffic the result is `state: unavailable` and no finding is written |
| Ticket pre-test: agent skill | ✅ | ✅ | the `ticket-pretest` skill — the agent's playbook (read the ticket and `pretest context`; plan checks before touching the device: acceptance points, then kit edge cases, then regression flows; begin/end each check; findings with expected vs actual and evidence refs; facts apart from guesses; pack; the verdict stated plainly), the QA review, drafts, repro and rerun, profile, warm-up, a11y and events, and the 18-case edge-case kit with setup verbs and platform limits (`references/edge-case-kit.md`, mirroring `pretest/kit.py`); registered in the `autonom` map and `project-router` |
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
| `scrcpy` (Android) | device H.264 over `/ws/video`, decoded by WebCodecs in the page and shown in order (at most two frames wait, one per screen refresh); up to 60 fps unless `--fps` caps it; `--max-size` is the longer side, `0` native, and without it the size follows the device's H.264 encoder (read once with `list_encoders=true`): 2048 with software encoders only (the emulator: about 58 fps, SSIM 0.981, about 30% less host CPU than native), native with a hardware one, 1280 when it cannot tell; `/status` `scrcpy.max_size` and `scrcpy.encoder` say which; `--bit-rate` defaults to 12 Mbit/s | `/ws/control`, as it happens: touch with hold and up to 10 pointers, scroll, key down/up with meta state, Unicode text and paste through the device clipboard, clipboard fetch, Back/Home/Apps/Power/Volume/Wake/Notifications/Quick settings/Collapse/Rotate, display presets (`{"t":"display"}`; the video follows the new size without a restart) | scrcpy-server 4.1; a browser with an H.264 `VideoDecoder` for the decoded picture, else the multipart picture |
| `screenrecord` (Android) | device H.264 → ffmpeg → MJPEG at `--fps` (default 15); `--max-size` is the width (default 1280, `0` is 4096) | HTTP: tap, swipe, allowlisted keys, conservative ASCII text, display presets (`POST /display`; the capture restarts at the new size) | ffmpeg; a device whose `screenrecord --output-format=h264` produces H.264 |
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
                     [--http-proxy HOST:PORT] [--port N]
autonom devices shutdown [--target ID]
autonom doctor [--strict] [--mitmdump PATH]
autonom tour [--run] [--avd NAME] [--flow PATH] [--human] [--shutdown]
autonom capabilities [--probe]

autonom session start [--app-id ID] [--install PATH] [--launch [ID]] [--activity C] [--log-stream]
                      [--alongside] [--started-by canvas:PORT:PID]
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
autonom ui swipe --from X,Y --to X,Y [--duration S]   # seconds (0.3 default), > 60 is invalid_value
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

autonom pretest start --ticket FILE|- --base REF [--head REF] [--repo PATH] [--flows DIR]
                      [--title TEXT] [--ticket-ref KEY] [--app-id ID] [--max-actions N]
                      [--max-minutes N] [--forbid-text PATTERN]... [--allow-app ID]...
autonom pretest status|context|stop|health|list
autonom pretest check add <title> --kind KIND [--case KIT-ID] [--why TEXT] [--flow PATH]
autonom pretest check begin <check-id> | end <check-id> --verdict V [--note TEXT] | list
autonom pretest flows [--env KEY=VALUE]... [--secret NAME]...
autonom pretest finding add --check ID --severity S --title T --actual T [--expected T]
                            [--evidence REF]...
autonom pretest finding list
autonom pretest pack [--out DIR]
autonom pretest review <pack> <approve|more-checks|return-to-dev> [--note T] [--check TITLE]...
                       [--finding ID]... [--confirm ID]... [--dismiss ID]... [--author NAME]
                       [--app-id ID] [--max-actions N] [--max-minutes N]
                       [--forbid-text PATTERN]... [--allow-app ID]...
autonom pretest review-status <pack>
autonom pretest kit
autonom pretest flow-draft <finding-id> [--pack PATH] [--out PATH]
autonom pretest repro <finding-id> --times N [--pack PATH] [--env KEY=VALUE]... [--secret NAME]...
autonom pretest rerun <pack> [--check ID]... [--env KEY=VALUE]... [--secret NAME]...
autonom pretest warmup [--env KEY=VALUE]... [--secret NAME]...
autonom pretest a11y [--check ID]
autonom pretest events [--check ID]

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
autonom canvas                   # a workspace Canvas: `canvas serve` with no target or device
autonom canvas serve [--port N] [--transport auto|scrcpy|idb|screenrecord|screencap] [--fps N]
                     [--max-size PX] [--bit-rate BPS] [--scrcpy-server PATH [--scrcpy-version X.Y]]
                     [--idb-companion PATH] [--token TOKEN] [--no-auth]
                     [--device PLATFORM:ID]... [--split] [--workspace NAME] [--ephemeral]
                     [--bootable avd:NAME[@PORT]|simulator:UDID]... [--shutdown-booted]
                     [--install-root DIR]... [--captures-dir DIR]
autonom canvas stop [--port N | --workspace NAME | --all]
autonom canvas attach [--port N | --workspace NAME] [--tab ID|NAME]
autonom canvas detach [--port N | --workspace NAME] [--device-id ID]
autonom canvas list
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

### Ticket pre-test (`autonom pretest`)

A pre-test lives inside the current session (`no_active_session` without
one) under `<session>/pretests/<pretest-id>/`, owner-only. `--kind` is
`ticket|edge|regression|network|a11y|analytics`; `--verdict` is
`pass|fail|blocked|not_covered|inconclusive`; `--severity` is
`blocker|major|minor|info`; `--case` is an id from `pretest kit`.
`--forbid-text` replaces the default list; `--allow-app` defaults to the
session's app. `--ticket -` reads the ticket from stdin (at most 1 MiB).
`pretest pack` without an active pre-test packs the session's newest one
(a stopped one is packed, a packed one is returned unchanged). The `pretest`
verbs are journaled but never count against the budget; the flow steps that
`pretest flows`, `pretest repro` and `pretest rerun` run do. `repro`
refuses while a check is open (`check_already_open`), `rerun` while another
pre-test is active (`pretest_already_active`), and `flow-draft` refuses to
overwrite a file with other content (`output_not_writable`). `warmup` runs
only with no check open (`check_already_open`) and refuses a profile flow
that does not exist (`flow_file_not_found`) before touching the device;
`a11y` and `events` take `--check` for any check of the pre-test
(`unknown_check`), and `events` needs the check to have begun and a profile
with `events` rules (`invalid_value` otherwise). Error codes:

| `error_code` | When |
| --- | --- |
| `no_active_pretest` | a pre-test verb with no active pre-test in the session |
| `pretest_already_active` | `start` (or `review … more-checks`) while another pre-test is active |
| `pretest_finalized` | a change to a packed pre-test, `pack --out` to a second place, or `repro --pack` (a pack is final; use `rerun`) |
| `check_already_open` | `check begin` (or `flows`) while another check is open (`open_check_id`) |
| `no_open_check` | `check end` for a check that is not open, including one already ended (same verdict or not; record more in a finding) |
| `unknown_check` | a check id the pre-test does not hold |
| `invalid_verdict` | a `--verdict` outside the five verdicts |
| `invalid_severity` | a `--severity` outside the four severities |
| `unknown_finding` | a finding id the pack does not hold |
| `evidence_ref_unknown` | an `--evidence` ref that names no artifact of the session (nothing written) |
| `budget_exhausted` | the action or minute budget is used up; no input was sent |
| `forbidden_action` | the tap target matches `--forbid-text`, or the screen cannot be read; no input was sent |
| `app_not_allowed` | `open`, launch, stop or clear of an app outside the allow list, or an Android `open` that would show the system app chooser (`chooser: true`) |
| `review_needs_finding` | `review … return-to-dev` without `--finding` |
| `not_automatic` | `--confirm`/`--dismiss` of a finding an agent wrote |
| `not_a_pretest_pack` | `review`/`review-status` on a directory that is not a pre-test pack |
| `report_integrity_failed` | `report verify` (or a review) of a pack with a changed, missing or unlisted file |
| `draft_unavailable` | `flow-draft` of a finding without a check, of a check that never began or has no replayable action, or whose session journal is not on this machine; `repro` of a finding without a draft; `rerun` of a check (or a pack) with no flow or draft on this machine — nothing is written |
| `profile_invalid` | an app profile that cannot be read, does not parse, has unknown keys, an `accounts` value that is not a variable name starting with `AUTONOM_` or `PRETEST_`, or a credential-shaped value (`failures` lists key paths, never values); refuses `start`, `warmup` and `events` |

Also used: `invalid_value` (an empty or missing ticket, a bad `--max-actions`,
`--max-minutes`, `--forbid-text` pattern, `--kind` or `--case`, `more-checks`
without `--check`, a child-rule flag such as `--max-actions` on `approve` or
`return-to-dev`, `events` without profile `events` rules, or `events --check`
of a check that never began) and `flow_file_not_found` (`check add --flow` to
a missing file, or a profile's `login_flow`/`warmup_flow` that does not exist).

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
