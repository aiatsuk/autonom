# Changelog

All notable changes to Autonom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as enforced by `scripts/validate_plugin.py` (the library version in
`scripts/autonom_lib/__init__.py` is the single source of truth).

## [Unreleased]

## [0.35.0] - 2026-10-10

### Added
- **Canvas workspace with several devices.** `autonom canvas` (or
  `canvas serve` with no target) starts a workspace Canvas with no device and
  no session that keeps running until Ctrl+C or `autonom canvas stop`. The
  page has in-page tabs (at most 8), each a split of 1-4 tiles with its own
  `/c/<tab id>` URL; a device lives in one tab, and other tabs offer "Move
  here". Attaching a device starts its own Autonom session (`session start
  --alongside`, never touching `current.json`) or reuses the live one on that
  target; detaching ends only sessions the Canvas started. The focused tile
  streams at full rate and the others at a background profile (device
  summaries and `/status` report the planned profile and, as
  `profile_applied`, the one the stream runs after a restart that worked); one device's
  crash restores that device only. Tabs and the devices attached at stop are
  saved per workspace (`--workspace NAME`, `--ephemeral`). New `canvas serve`
  flags: `--device PLATFORM:ID`, `--split`, `--workspace`, `--ephemeral`,
  `--bootable avd:NAME[@PORT]|simulator:UDID`, `--shutdown-booted`,
  `--install-root DIR`, `--captures-dir DIR`; `--port 0` picks a free port.
  New verbs `canvas stop`, `canvas attach`, `canvas detach` and `canvas
  list`, and the codes `canvas_not_found`, `canvas_ambiguous` and
  `workspace_in_use`. A second `autonom canvas` for a running workspace
  prints its URL instead of starting another.
- **Canvas Actions drawer.** Per device: screenshot, recording (up to
  180 s), install from `--install-root` folders only, launch, open link, app
  language, a captures gallery in `~/Downloads/Autonom/<device>/` (200 files
  or 2 GB per device, oldest deleted with a notice), the command log and
  tool health.
- **Mobile Canvas Tools drawer.** A Tools button in the Canvas toolbar opens
  a drawer with five tabs for the Canvas's own target. It takes the
  inspector's column on a desktop-width page (the two are never open
  together) and is a full-width sheet at phone width; keyboard tabs, light
  and dark. App: the app id (from the session on this target or the
  best-known app, editable); Android lists the app's requested runtime
  permissions with their granted state and grants, revokes or resets one,
  iOS grants, revokes or resets a privacy service and says it cannot read the
  state back; location by latitude and longitude or a preset, with an honest
  Android read-back (delivered, or requested only until an app subscribes),
  Clear on iOS only (the emulator has no reset). Simulate: push (iOS),
  biometrics, battery, network online/offline (Android) and appearance, only
  what the platform supports. Network: status, start after a confirmation
  that names the MITM effect, attach, detach, stop, a live request list with
  host, method, status and mocked filters, redacted previews only, and Mock
  this; it needs an Autonom session on this target and never starts one.
  Mocks: list with hit counts, add, edit, enable, disable, remove and clear
  after a confirmation, with the note that mocks apply to every Autonom
  session on this Mac. Logs: the live device log, optionally for one
  package, with level and text filters, pause, clear and at most 2000 rows
  (on iOS only when the session on this Simulator has `--log-stream`).
- **Canvas tools routes.** `POST /tools/call`, `GET /tools/logs` and
  `POST /tools/logs` behind the Canvas authorization and CSRF, served by a
  separate NDJSON tools process (`scripts/autonom_canvas_tools.py`) and one
  `logs follow --source device` child per Canvas (2000 lines / 1 MiB ring,
  stopped 60 s after the last read, on close and at Canvas stop; an optional
  `match` text keeps only the lines holding it, at the source). Device
  changes follow the handoff rule (403 `control_refused`), a call waits at
  most 60 s (504 `timeout`), a gone tools process answers 502
  `tools_unavailable` and is restarted at most once per 10 s, and input and
  the stream never wait for it. Every change is journaled as `canvas <op>`
  with its origin, without mock bodies, push payloads or log text. `/status`
  adds `tools`.
- **Live check** `tests/live/canvas_tools_live.mjs` drives the real Canvas
  `/tools` routes on emulator-5580 or the Autonom-Fast-Test Simulator with a
  temporary `AUTONOM_HOME`, checks each panel's device effect, restores what
  it changed, and with `--ui` checks the drawer in headless Chromium at
  1440x900 and 390x844, light and dark.

### Changed
- `autonom canvas serve` with no target or device flag now starts a workspace
  Canvas instead of picking the session's target or the only ready device.
  Pass `--serial`, `--udid`, `--target` or one `--device` for the
  single-device Canvas, which is unchanged (same node command line, page,
  routes and process owner).

## [0.34.0] - 2026-10-06

### Added
- **iOS Canvas volume buttons.** On the idb transport the dock's Volume down
  and Volume up (and `{"t":"system","op":"volume-up"|"volume-down"}`) press
  the Simulator's own VOLUME_DOWN and VOLUME_UP buttons; the Mac's volume is
  untouched. The journal bridge maps `KEYCODE_VOLUME_UP`/`KEYCODE_VOLUME_DOWN`
  to them for iOS and presses them with `idb ui button` (the `ui` key path
  takes only the buttons every iOS input backend has), and HTTP `POST /key`
  now accepts both names on either platform.
- **iOS Canvas clipboard.** On the Simulator's own Mac, `clipboard-get` reads
  the Simulator clipboard (`xcrun simctl pbpaste <udid>`; empty answers no
  text) and `paste` sets it (`xcrun simctl pbcopy <udid>`) at its place in the
  input order, then types the text when it fits idb's text path: plain ASCII
  (printable characters and newline) of at most 300 bytes. Other text is only
  set, and the reply says to touch and hold a text field and choose Paste,
  since Cmd+V through idb does not paste text set from the Mac. The page
  sends a paste, and text longer than 300 bytes or not plain ASCII, as a paste
  when the `state` message says `clipboard: true`. Clipboard text is never
  logged or journaled (one `paste` record with its length). A Canvas not
  running on macOS refuses both with a message and runs no simctl command.
- **iOS Canvas pinch.** A second pointer of the same connection, or the
  page's Ctrl/Alt-drag (now also on iOS), makes a pinch that is sent once,
  when either pointer lifts, as idb's canned HIDPinch: center at the midpoint
  where the pointers started, radius half their start distance, scale end
  distance / start distance, and the time they took, at most 2 s. idb 1.6.4
  has no live second finger, so nothing moves until the release and there is
  no two-finger pan or rotate. The page's mirrored finger comes first, so a
  Ctrl/Alt pinch sends no touch down or up at all; two real fingers that land
  one after the other tap at the first one's spot before the pinch, since
  every single finger is live. One `gesture` record with `pointers: 2`. A
  pointer from another connection, or a third one, is still refused. The idb
  client gains `encodeHidEvent({ pinch })` (HIDEvent field 4) and
  `IdbHidStream.pinch()`.
- **Live check `controls`.** `tests/live/canvas_ios_live.mjs --case controls`
  presses Volume up and down through the control socket and reads the
  Simulator's `sim_volume` rise and return, pastes `autonom-clip-test` and
  reads it back, and sends a mirrored pinch then a tap, checking the journal.
- **Android Canvas stream size follows the encoder.** Without `--max-size`,
  the scrcpy transport runs the pushed scrcpy-server once per device with
  `list_encoders=true` (at most 3 s, the answer kept for restarts) before its
  first stream: a device with only software H.264 encoders (every emulator)
  streams at most 2048 on the longer side, one with a hardware H.264 encoder
  (phones) streams native size, and a list that fails or says neither keeps
  1280. `--max-size N` still wins and no probe runs; `--max-size 0` (also in
  `autonom canvas serve`) is native size, and the screenrecord stream reads
  it as 4096 wide. The Canvas prints the chosen size, `/status` `scrcpy`
  reports `max_size`, `max_size_source`, `encoder` and `encoder_name`, and
  the Android `state` message carries `max_size` and `encoder` of the
  transport in use (after a fallback to screenrecord its width, with the new
  `/status` `screenrecord_max_size`; `scrcpy` then names no size). On the API 36
  emulator (`-gpu host -cores 8`) 2048 (912x2048) gives about 58 fps, 0.981
  SSIM against a native screenshot and about 30% less host CPU than native;
  the live bench presented a median of 57 fps at the new default. The live
  check `tests/live/canvas_scrcpy_live.mjs` gains `--max-size` and records
  the chosen size and encoder in its bench report. The performance notes
  replace the 52-54 fps native figure with the measured table.

### Fixed
- **An empty Canvas number flag is refused.** `--max-size ""` (and any other
  integer flag of the canvas server given an empty value) was read as 0; it
  is now an error, since 0 means native size.
- **A Canvas page that fell behind always gets the current pause and owner
  state.** A page more than 64 KiB behind skips state messages and was caught
  up only when its socket emptied, or before a video packet if it was by then
  under 64 KiB again. A page on a slow link whose backlog never emptied could
  keep showing an old state (for example "paused" after input resumed). It
  now gets the current state ahead of the next video packet or control reply
  it is sent, whatever its backlog, and states are skipped only while the
  socket owes a drain event, so a socket with a higher high-water mark than
  Node's default still catches up. The journal flood test's waits now run on
  while the Canvas makes progress and report which client fell behind.

## [0.33.0] - 2026-10-05

### Added
- **iOS Canvas at 60 fps (idb transport).** On an iOS Simulator `canvas serve
  --transport auto` now streams the Simulator's own H.264 at up to 60 fps and
  full resolution into the WebCodecs page when idb_companion is found
  (`--idb-companion PATH`, the new `AUTONOM_IDB_COMPANION_BIN`, or `PATH`;
  `AUTONOM_IDB_COMPANION` keeps its meaning, the remote companion of idb
  calls, and is never read by the Canvas; `--transport idb` requires a
  usable binary and refuses with `tool_missing`, `capability: "canvas.idb"`,
  from the CLI and from the canvas server alike). The Canvas starts and owns one companion on
  a private socket, shares its one stream with every tab (a late tab starts
  at the cached key frame; a key frame every 2 s, forced by Stop and Start
  only when one does not come), always ends a stream with Stop, restarts the
  companion after a stream ends without one (also when a forced restart's
  Stop is not written or not answered, before any new Start, so two encoders
  never run), restarts the stream when the
  Simulator boots again, and stops 15 s after the last page. The page shows
  the decoded frames in order, one per screen refresh with at most two
  waiting, so a frame that arrives together with the next one is no longer
  lost (Android does the same, see Changed). The page turns
  the portrait picture for the Simulator's orientation (`orientation` and
  `rotation` in `state` and `/status` `idb`). Input on `/ws/control` is live:
  one finger over the companion's HID stream (a second pointer is refused),
  the wheel as a short drag that never taps (it starts inside the screen and
  always moves past the tap slop), Home and Power as Home and Lock, and text
  through the action bridge, all in one ordered queue, so input sent after
  text waits until it is typed, under the same authentication and handoff
  rules as Android (pause and takeover are checked again when each queued
  item runs); a finger or button held down when the companion is lost is
  lifted through the next companion; each
  completed action is one journal record with `transport:
  idb`. Without idb_companion, or when the stream cannot start, the Canvas
  falls back to screenshots and `/status` `fallback_reason` says why.
  `tests/live/canvas_ios_live.mjs` measures decoded, dropped, presented and
  distinct-frame fps in headed Chromium (a static control window, then
  Settings flung for 10 s), frame intervals, host load, SSIM outside the
  screen's rounded corners and camera cutout, sharing and input on a test
  Simulator.

### Changed
- **Android Canvas at 60 fps.** The scrcpy page now shows decoded frames in
  order like the iOS page: at most two wait, one is drawn per screen refresh,
  and only a third waiting frame is dropped (keeping only the newest lost
  10-20% of frames). The default `--bit-rate` is 12 Mbit/s instead of 8
  (scrcpy, idb and screenrecord), so a frame at 60 fps gets about 90% of the
  bits it had at about 36 fps and 8 Mbit/s (8 Mbit/s would leave 60%). The `bench` case of
  `tests/live/canvas_scrcpy_live.mjs` now drives the finger from a 16 ms
  timer, measures in headed off-screen Chromium through Playwright
  (`--headless`, `--playwright`, `--seconds`), and reports device-produced,
  received, decoded, presented and distinct fps per second, latency and host
  load; it passes on a presented median of at least 55 fps with a distinct
  median of at least 45. On the API 36 test emulator (`-gpu host -cores 8
  -memory 4096`, 570x1280 video) it measured 59.4 device, 58.9 decoded, 59
  presented and 59 distinct fps, none dropped, and 99 ms median (117 ms p95)
  from touch to picture.

### Fixed
- **iOS Canvas shows the screen on Xcode 27.** Since Xcode 27, `simctl io
  screenshot -` writes a file named `-` in the working directory instead of
  PNG on stdout, so the iOS Canvas sent no frames and left a stray `-` file.
  The Canvas now captures into a private temporary file and removes it.
- **iOS Canvas can press Home.** The page's Home and Power buttons sent
  Android key names that iOS refuses, so the iOS page hid its buttons and a
  Simulator had no Home from the Canvas. The bridge now presses the
  Simulator's Home and Lock buttons for them, and the iOS page shows Home and
  Power; the Android-only buttons stay hidden.

## [0.32.0] - 2026-10-04

### Added
- **Mobile Canvas display presets (Android)** — the Canvas switches its
  target's screen size and density live, with `wm size` and `wm density` on
  its own serial, while the emulator, the Canvas, its device server and open
  pages keep running: `small` (Small phone, 720x1280 @ 320), `pixel-11`
  (Pixel 11, 1080x2424 @ 420), `pixel-fold` (Pixel Fold open, 2208x1840 @
  420), `tablet` (Tablet, 2560x1600 @ 320) and `default` (the device's own).
  A person picks one from the Size menu in the page toolbar; an agent sends
  `{"t":"display","preset":...}` on `/ws/control` or `POST /display`, under
  the same authentication and handoff rules as input. An unknown preset, iOS,
  a paused Canvas or another owner is refused without running any `wm`
  command; changes run one at a time, a newer waiting request supersedes the
  older one, and held fingers and keys are lifted first. scrcpy follows the
  new size without a restart, screenrecord restarts its capture. `/status`
  `display` and the `state` message gain `preset`, `density` and `presets`,
  read from the device at most every 2 s; each change is one `ui display`
  journal record. When it stops on SIGINT, SIGTERM or SIGHUP, the Canvas puts
  back the size and density it found before its first change (a Canvas killed
  with SIGKILL cannot; the skill gives the `wm size reset` / `wm density
  reset` commands). The page itself is rebuilt to an Apple-style layout: a
  slim toolbar, the device in a frame that follows the video, a floating pill
  of device buttons and an inspector, light and dark themes, and a phone
  layout with 44 px targets; element ids and `window.autonomCanvas` are kept.
  Compatibility notes are in `docs/COMPATIBILITY.md`.
- **iOS UI recovery through an optional XCUITest backend** — ported from the
  September `fix/ios-ui-recovery` work onto the AXe input routing. A new
  `--ui-backend auto|idb|xcuitest` (also `AUTONOM_UI_BACKEND`, and stored by
  `session start --ui-backend`) chooses who serves the iOS tree and input.
  `auto` (the default) keeps idb, with `--ios-hid auto|idb|axe` routing its
  input exactly as before; only when an app session's idb tree is empty, has
  no usable root frame, or fails does it switch to the bundled targetless
  UI-test runner (`native/ios`), and it records `fallback_reason`
  (`empty_accessibility_tree`, `invalid_accessibility_geometry`, or the idb
  error code). The runner serves tree, find, selector and coordinate taps
  (by element reference when the node came from its own tree), long press,
  swipe, text and Home through a nonce-bound mailbox in the Simulator
  container; it needs full Xcode, builds on first use (cached by source and
  Xcode version; builds with Xcode 27), exits after five idle minutes, is
  registered to the session with a process signature, and is stopped by
  `session stop`. Every runner input is sent once: a lost answer is
  `ui_action_uncertain`, never repeated and never re-sent through idb or AXe.
  iOS results and flow events carry `ui_backend`, `input_backend`,
  `fallback_reason` and `geometry` when a UI read or input measured them, and
  the input verbs report `backend: xcuitest`.
- **Screen geometry is validated, not guessed** — a zero or missing root
  frame is no longer a screen and child extents are no longer taken as its
  size; Autonom then measures Simulator points from the device type's screen
  scale (`capabilities.plist` on Xcode 27, `profile.plist` before) checked
  against a fresh screenshot's pixels and orientation. While the XCUITest
  runner serves the session, an input whose screen size cannot be established
  is refused before dispatch with `display_geometry_unavailable`; the idb/AXe
  route still sends it unguarded, as before.
- **`--session-id` on every device verb** — binds one invocation to a
  session without moving the current pointer. A stopped session is accepted
  only by read-only verbs (`journal`, `shots`, `logs`, `report` except
  `report serve`, `session show|outputs`; others answer `session_stopped`;
  its journal and `session.json` never change after the stop), another
  target's flags
  answer `session_target_mismatch`, and `session start` refuses it. `session
  stop` is journaled after teardown. Journal appends allocate sequence numbers
  under a file lock.
- **Measured UI readiness** — `capabilities --probe` reads the current UI
  once; a UI read in the last minute refines the installed-tooling view of
  `ui.accessibility` (a degraded tree and its reason), and while the runner
  serves a session `ui.input` is the runner's. Flows that require
  `ui.accessibility` or `ui.input` on iOS measure once in preflight.
- New additive error codes: `session_stopped`, `session_target_mismatch`,
  `display_geometry_unavailable`, `ui_action_uncertain`, `app_id_required`,
  `ui_bridge_in_use`, `xcuitest_unavailable`, `xcuitest_build_failed`,
  `xcuitest_start_failed`, `xcuitest_timeout`, `xcuitest_failed`,
  `stale_ui_element`, `no_focused_field`.
- Reader fixture flows for a durable offline draft and a cancelled Google
  permission prompt (`examples/flows/`), and `tests/live/ios_ui_recovery_live.py`,
  the live check of the runner on an isolated simulator. The release archive
  now carries `native/` and `examples/`.
- The live check now also drives text entry, long press, swipe and Home
  through the runner in Settings, each with its own pass/fail field and an
  observed outcome (the field's value, the text edit menu, newly revealed
  rows, Settings leaving the runner's tree); of the typed text only its
  length reaches the evidence. Its `ok` also requires the simulator to end in
  the state it started in and the expected Xcode major version
  (`--expect-xcode-major`, default 27). The idb baseline now runs first, on
  the fresh boot.

### Fixed
- **A runner request racing `session stop` no longer starts a runner after
  the stop (#31).** `session stop` now writes `stopping_at` on the session
  record, atomically and under the record lock, before its first teardown
  step. A runner request refuses a stopping or stopped session with
  `session_stopped` when it starts, again under the Simulator's request
  lock, and once more right before it would launch a new runner (after the
  build, which takes at least a second); the checks read the session's own
  `session.json`, so a stop that already removed the current pointer is
  seen too. A stop that aborts before the session is stopped (a failed
  accessibility restore keeps the pointer) withdraws only its own owner token
  (`stopping_tokens`): the mark stays while another `session stop` is still
  tearing down, and once none is left the session stays usable.
  `session.save` keeps whatever mark the file holds, so a
  command holding an older copy neither drops nor revives it. Records from
  earlier versions have no mark and behave as before. Closes #31: the window
  in which a request could pass both checks and start a runner registered to
  a stopped session is gone, so that runner no longer waits out its
  5-minute idle timeout or needs `autonom cleanup`.
- An unanswered runner tap under `--ui-backend auto`, in an invocation that
  follows the session's persisted runner observation, is covered by a
  regression: one runner request, `ui_action_uncertain`, nothing through idb
  or AXe.
- **Canvas Upgrade fallback accepts a zero `Content-Length` (#20).** On Node
  versions that ignore `shouldUpgradeCallback`, a bodiless request with an
  `Upgrade` header and `Content-Length: 00` got a 400. Any run of zeros now
  means no body; a signed, empty or non-numeric length is still refused.
- Steadier Canvas tests on busy CI runners (#22): the input tests allow up
  to 10 s (was 3-5 s) for multi-step device exchanges such as clipboard
  holds, the journal flood test gives a lagging client up to 10 s to catch
  up, and state messages serialize the presets list once.

## [0.31.2] - 2026-10-03

### Fixed
- Redraw the plugin's directory icon as a rounded chip with transparent
  corners and the glyph centred at 76% fill, so it fills its tile and reads as
  an app icon on dark and light listings instead of a small mark in a white
  box. The file shrinks from 770 KB to 88 KB.
- Rename the scrcpy version pattern constant in `scrcpy-lib.mjs` from
  `VERSION_TOKEN` to `VERSION_NUMBER`. It matches version numbers, not
  credentials, and the directory scanner read the old name as a credential
  read. No behaviour change.

## [0.31.1] - 2026-10-03

### Fixed
- Declare the compact app-and-orbit PNG as the plugin's directory icon so it
  appears on the first public listing submission.
- Add documentation, support, and privacy links to the directory manifest.

## [0.31.0] - 2026-10-03

Deterministic capture state and a repair hand-off, borrowed from
[goldie](https://github.com/kacperkapusciak/goldie) — an App Store screenshot
generator that replays flows on the same simulators and emulators and had
already solved the noise that makes two captures of one screen differ.

Then a hardening pass: every feature exercised again on this Mac's
emulators and simulators and against fakes, with a regression test for each
defect found. It brings AXe as an iOS input backend for Xcode 27 (where an
older idb companion can no longer inject touches), agent ergonomics seen in
competing tools (`ui wait --settled`, an outline tree, ranked repair
candidates, did-you-mean hints), honest `verified` read-backs on every
simulator control, and a round of secret hygiene on device input, the
journal, captured URLs, and approvals.

Then a fix round from driving a real Flutter app (WoolBox) on a dedicated
Android emulator and iOS simulator: off-screen nodes, iOS logs of a Flutter
`Runner`, process ownership, attachment evidence, flows, and metrics, each
with a regression test against the fakes. The non-additive changes are
listed in `docs/COMPATIBILITY.md`.

Then the Mobile Canvas scrcpy transport: on Android the Canvas streams the
device's own H.264 from scrcpy-server 4.1 into a WebCodecs page and sends
touch, wheel, keys, text and the clipboard to the device as they happen,
without ffmpeg and without an npm dependency. Its compatibility notes are in
`docs/COMPATIBILITY.md` too.

### Added
- **Transparent Android network capture, no app change** — the fix for
  live WoolBox (a Flutter/Dio app) capturing zero requests: `dart:io` ignores
  Android's global `http_proxy` and apps targeting API 24+ do not trust
  user-store CAs, so the device-proxy + user-CA path saw nothing. Now
  `devices boot --avd X --http-proxy HOST:PORT` launches the emulator routed
  through the proxy at the network level (a launch-time flag; it cannot be
  applied to a running emulator), and `network attach --system-ca` installs
  the MITM CA into the SYSTEM trust store of a rooted `google_apis` emulator —
  reversibly (a tmpfs over the store plus a bind into each zygote mount
  namespace, cleared by a reboot) and API-aware (the APEX conscrypt store with
  the `nsenter` bind on API≥34, `/system/etc/security/cacerts` below). The
  install is verified from the zygote mount namespace, not a plain `adb shell`,
  and the real result rides on `system_ca.verified` / `checked_via`; a failed
  verify is a non-fatal `system_ca_unverified` warning, not a false success.
  Because the emulator is routed through the proxy at launch, every flow on the
  proxy is counted as the device's (all arrive from `127.0.0.1` by design; a
  host process using the port would be counted too, acceptable on a dedicated
  test host): `network status` answers `attached: true`, `evidence:
  transparent_proxy`, `unattributed_flow_count: 0`, and `network requests` lists
  all flows. That holds only while the process registry still shows the
  emulator booted routed through the proxy; after `devices shutdown` or an
  unrouted reboot, status answers `attached: false`, `evidence:
  transparent_route_gone` (unless a guest-network `10.0.2.x` flow still proves
  attachment), and loopback flows are unattributed again.
  `--system-ca` is refused (`reason: app_proxy_attached`) while an app-proxy
  attach is active, so the device's saved proxy is never lost; `network detach`
  first. The session records
  `capture_mode: "transparent"`; `network status` also reports the installed
  `system_ca`. It refuses cleanly — `unsupported_capability` with a `capability`
  extra — on a non-rootable / Play image (`network.system_ca`) or when the
  emulator was not booted proxy-routed (`network.transparent_capture`, with a
  hint to reboot). Verified live on API ≥ 34 (APEX conscrypt); the API < 34
  `/system/etc/security/cacerts` remount is implemented but not yet
  device-verified. The device-proxy + user-CA path stays as the fallback for
  non-rootable devices, now marked `capture_mode: "app_proxy"`, with the honest
  warning that Flutter and pinned traffic are not captured that way. A recorded
  flow's `host` is now the requested name (mitmproxy's `pretty_host`: Host
  header / `:authority`) instead of the CONNECT target, which is an IP under
  `-http-proxy`; host-based mocks match on the same name, so `network requests
  list --host` and `network mock add --host` work in transparent mode. The IP
  stays available as an additive `server_ip`.
- **`autonom tour`** — the guided first run: what the harness has (verb
  families), the usual workflow, an inventory of this Mac's emulators and
  simulators, and an offer to boot one, own a session, and walk three
  screens into the Settings app (Android: Network & internet → Internet;
  iOS: General → About) with a screenshot, UI hierarchy and device log at
  every step, an HTML/JUnit report, an integrity bundle, and a written
  account (`tour.md`). `--run` performs it (a TTY is asked first, in
  English), `--human` prints the Markdown instead of JSON, `--flow` walks
  your own flow, `--shutdown` powers off what the tour booted. The walk is an
  ordinary Flow v1 file under `scripts/autonom_lib/tours/`, run with evidence
  mode `always`: before/after frames, hierarchy, and logs stay attached to
  the action that produced them, so a click is one report step rather than a
  click followed by a duplicate screenshot step, and every tap or back is
  followed by an assertion that the screen it left is gone. `--run` refuses
  while a session is active (`session_already_active`), refuses an unknown
  `--avd`, a platform/target mismatch, and several candidates without a
  target (listing them), and on iOS checks that the tree and input backends
  are ready (idb, or AXe for input) before it boots anything. Whatever
  fails after boot — a flow failure, an exception, Ctrl-C — the tour's
  session is stopped and `--shutdown` is honoured. Reports are written
  `0600` and the Next hints are commands that work once the tour is over.
- **`simulator status-bar pin`** on both platforms: full battery and
  signal, no notification icons, and the real ticking clock, so a
  before/after screenshot diff shows only what the app changed. iOS uses
  `simctl status_bar override` (no `--time` unless `time=9:41` is given).
  Android defaults to a *live* mode — battery through `dumpsys battery`,
  cellular bars through the emulator console, notification icons through
  `cmd statusbar send-disable-flag` — because SystemUI demo mode freezes
  the clock at the moment it is entered (measured: 73 s later the bar still
  showed the entry minute). `hhmm=0941`, `wifi=`, `mobile=`, `datatype=`, or
  `mode=demo` switch to demo mode knowingly; `override` always uses it.
  `clear` undoes both modes. A SystemUI without the disable flag is reported
  with `status_bar_notification_icons_unsupported` rather than assumed.
- **`simulator keyboard pin|reset|show`** (iOS): autocorrect, prediction, and
  auto-capitalisation off and the locale set, written with `plistlib` into
  the shut-down simulator's preference store and read back for `verified`.
  `pin` snapshots the values it replaces under
  `$AUTONOM_HOME/simulator-prefs/<udid>.json` and `reset` restores them
  (a region-override locale such as `en_US@rg=nlzzzz` survives a pin/reset
  round trip); without a snapshot, `reset` deletes the owned keys.
  `reboot=true` cycles a booted device; a booted one without it is refused
  with the new `simulator_must_be_shutdown`; a device with no data directory
  with `simulator_data_not_found`. Android refuses with
  `unsupported_capability` because the settings live inside Gboard.
  `AUTONOM_CORESIMULATOR_DEVICES` relocates the Devices directory.
- **Flow repair brief:** a test failure in `flow run` carries `repair` — the
  `--until-step` prefix replay, `ui tree`, the old selector as a widened
  `ui find … --mode contains --all`, a labelled screenshot, and the
  re-verification commands, plus advice keyed by the error code and the
  events path as evidence. Definition/infrastructure failures get none.
- `screenshot`, `shots show`, and the shot index report `width`/`height`
  from the PNG header, so an agent knows the capture's coordinate space.
- `devices` adds `avd_profiles` (hardware profile, screen size and density,
  API level, ABI from each AVD's ini files; nulls when unreadable) and names
  the `avd` a running emulator booted from via its console.
- `doctor` reports every active `AUTONOM_*` override under `overrides` and
  warns with `override_path_missing` when a binary override points at
  nothing — the stale-environment trap that made a present tool read as
  missing.

- **Device sweep fixes (every verb exercised on a real emulator and
  simulator, 2026-09-02):**
  - Argument errors are one JSON envelope with `error_code: usage_error`
    and the usage line in `hint`, at every parser level — never argparse
    prose (an unknown verb, `ui tap --all`, `session launch --arg --es`).
  - `simulator biometric` on iOS works: `xcrun simctl` has no `biometric`
    subcommand (it had never worked); enroll/unenroll/match/nonmatch now
    post the Simulator's Darwin notifications through `simctl spawn
    notifyutil`.
  - `ui type` warns `no_focused_field` when nothing on screen reports
    keyboard focus, and the flow `inputText` command polls for a focused
    node (up to `timeoutMs`) and fails with `flow_no_focused_field`
    instead of typing into nothing and passing. `requireFocus: false` opts
    out.
  - idb verbs that die with a companion "Connection refused" prune stale
    companion registrations (`idb list-targets`) and retry once — the
    failure mode that took every idb verb down for the rest of a session.
  - `file ls`/`file pull` on a release or system app refuse with
    `app_not_debuggable` instead of listing run-as's complaint as a file.
  - `ui tap` with neither a selector nor coordinates refuses with
    `selector_required` instead of `ambiguous_selector: matched 78 nodes`.
  - `capabilities` no longer needs a session: with `--serial`/`--udid` (or a
    sole ready target) it snapshots that target.
  - `doctor` warns `device_attached_to_foreign_proxy` for a running
    emulator whose global proxy points at another session's live proxy —
    the one case `device_may_be_left_attached` cannot see because the proxy
    is alive.
  - `ui tree` reports `truncated` when `--max-nodes` cut the list and
    `screen` (the bounds' coordinate space) for live trees; the
    `coordinate_space_mismatch` hint is per platform.
  - `flow run --dry-run` lists `planned` steps; flow error messages and the
    repair brief show resolved env values (`text: bluetooth`, not
    `text: ${QUERY}`) unless a secret is involved.
  - `location set` on the Android emulator says `delivery:
    on_subscription`: the fix reaches the location manager only once an app
    subscribes, so `location get` cannot confirm it.
  - Flow `launchApp` starts the app fresh (launcher activity on a cleared
    task on Android, terminate-then-launch on iOS); `resume: true` keeps the
    old resume-where-it-was behaviour. `session launch --fresh` does the
    same from the CLI. Measured cause: a resumed task put a flow's first
    selector on a subscreen or, with Android Settings, in another package's
    search activity that `stopApp` never touches.
  - `teach approve --run` performs the required consecutive replays itself
    instead of only counting past ones.
  - `flow export --format maestro` exports a timed `assertVisible` /
    `assertNotVisible` as `extendedWaitUntil` with the timeout instead of
    refusing.
  - `location get` on Android reports `requested` and `delivered` against
    the fix this session set, so a Googleplex answer after `location set`
    reads as "not delivered yet", not as a broken set.
  - A UIAutomator dump cut short by a screen transition is retried once and
    then refused as `backend_failed`; any remaining bare `ValueError` in a
    verb answers with `error_code: invalid_value` instead of no code at all.
  - `metrics snapshot` on Android reads the app's CPU line even when
    `dumpsys cpuinfo` prefixes the percentage with a sign (` +0% pid/app`),
    which an API-37 emulator does; it used to report `cpu_unavailable`.
  - `file ls` on iOS names a system app's missing data container
    (`app_not_installed`, simctl's literal `(null)`) and a missing path
    inside a real container, instead of listing nothing.
  - `doctor` no longer reports a live proxy owned by another session as an
    orphan *and* as foreign at the same time.
  - `record stop` with nothing recording answers `was_recording: false`
    with `path: null` instead of a phantom `latest.mp4`; `devices shutdown`
    on a stopped simulator reports `already_stopped`; `network requests
    list --mocked` is a bare flag; the xctrace "not supported on this
    platform" failure says to use a physical device.

- **AXe as the iOS input backend.** Xcode 27 moved `SimulatorKit.framework`
  to `Contents/SharedFrameworks`, and an idb companion built before that
  still looks in `Library/PrivateFrameworks`, so every touch fails while the
  accessibility tree keeps working. `ui tap`, `ui swipe`, `ui type`,
  `ui key`, and the flow `longPressOn`/`doubleTapOn` commands can now go
  through [AXe](https://github.com/cameroncooke/AXe)
  (`brew install cameroncooke/axe/axe`): `--ios-hid axe` /
  `AUTONOM_IOS_HID=axe` forces it, the default `auto` uses idb and switches
  to AXe only when idb's HID is known broken and AXe is installed, and
  `idb` pins idb with no fallback. `--axe PATH` / `AUTONOM_AXE` names the
  binary. The tree stays on idb. Typed text reaches AXe through stdin, so a
  leading dash is never read as a flag. Every `ui` input payload names its
  `backend` (`adb`, `idb`, or `axe`).
- **`doctor` understands Xcode 27:** it reports where SimulatorKit is, the
  `idb_companion` build, and whether AXe is installed, and splits iOS
  readiness into `capabilities.ios_tree` and `capabilities.ios_hid`. A
  companion that cannot load SimulatorKit marks HID not ready with the fix
  (`brew update && brew upgrade idb-companion`, or install AXe) in the
  warning; an invalid `AUTONOM_IOS_HID` is warned. An idb failure that says
  SimulatorKit is required for HID surfaces as the new
  `ios_hid_framework_missing` with the same fix.
- **`simulator animations pin|reset|show`** (Android): the window,
  transition, and animator scales to 0 so a capture never lands mid-animation;
  `pin` snapshots what it replaces and `reset` restores it (a scale that was
  unset is deleted again). iOS has no host-level switch and refuses with
  `unsupported_capability`.
- **`ui wait --settled`** polls the tree until two consecutive snapshots
  match for `--quiet-ms` (default 500), bounded by `--timeout-ms`; it
  reports `settled`, `snapshots`, `changes`, and `elapsed_ms`, and exits 1
  with a `screen_not_settled` warning when the screen never held still.
- **`ui tree --format outline`** prints one indented line per node instead
  of the nodes array, and `--interactable` keeps only nodes an agent can act
  on (buttons, fields, switches, …).
- A `ui find`/`ui tap` `--text` miss whose value matches a node's `desc`
  says so (`label_is_in_desc` warning, a `--desc` hint): the visible label
  lives in `desc` on iOS, and in the content-description of Flutter (and
  image button) labels on Android.
- **Repair `candidates`:** when the failing step's hierarchy was captured,
  the repair brief ranks up to five on-screen nodes by similarity to the
  failed selector, each with a ready Flow `selector` (and the `index` that
  picks it when a label is shared) and the `ui find` query that confirms it;
  for `ambiguous_selector` it lists every match with its distinguishing
  fields. The flow is never rewritten.
- **Did-you-mean hints:** an unknown flow command, argument, selector
  field, or match mode names its closest legal spelling (`Did you mean
  'tapOn'?`) ahead of the full list.
- `flow fmt --write --drop-comments` rewrites a file even though canonical
  output loses its comments; the preview reports how many would go.
- `teach approve --run` accepts `--env KEY=VALUE` and `--secret NAME` for the
  replays it performs.
- The iOS `session start --log-stream` file is filtered to the app's
  subsystem or executable (narrowed to the app by binary UUID when it is
  read) and capped on disk: `AUTONOM_IOS_LOG_MAX_MB` (default 50) with one
  rotation.
- JUnit export and `report suite` emit `errors` and an `<error>` case for a
  run that aborted on a definition or infrastructure error, and the suite
  JSON carries the matching broken and JUnit counts.
- `proof --base` selects a parent flow when only a `runFlow` child changed,
  counts untracked files as changes, lists flows that fail to load under
  `invalid_flows` instead of skipping them, marks a pass that leaves changed
  files uncovered as `partial`, and prints repo-relative paths.
- `atlas update --app-id X` ingests only screens whose foreground package
  is X (no edge spans a foreign screen) and records `last_seen` from the
  observation time; `atlas diff` refuses a file that is not an atlas.
- Android `permissions grant|revoke` accept short names (`camera`,
  `location` for the whole group) mapped to `android.permission.*`;
  `permissions reset` reports `scope` and the `revoked` list.
- Emulators started by `devices boot` are registered as harness-owned, so
  `doctor` no longer lists a running one as an orphan, and `devices
  shutdown` releases the entry.
- A failed `session start --install/--launch` rolls the new session back and
  names it in `session_rolled_back`.
- Every `simulator` control reports `verification` (`read_back`,
  `mismatch`, `unavailable`, `unsupported`) next to `verified`; an
  Android live status-bar pin carries a `signal_unstable` warning because
  the emulator's signal cannot be read back.
- `network status` adds `attach_state` (`automated`, `manual`,
  `not_attached`) alongside the unchanged `attached`.
- Suite discovery reports skipped non-flow YAML files as a
  `flow_files_skipped` warning with the reason, and `flow run` warnings reach
  the run summary.
- `AUTONOM_IDB_STATE_FILE` points at the idb client's companion registry.
- `session launch --fresh --activity C` starts that activity on a cleared
  task.
- **Flow `waitForSettled`** (declared `since` 0.31.0): polls the UI tree
  until it holds still — the engine of `ui wait --settled` — with
  `timeoutMs` (default 5000) and `quietMs` (default 500, capped at
  `timeoutMs`); it never fails the flow, and a tree still moving at the
  deadline is `settled: false` plus a `screen_not_settled` run warning.
  Maestro's `waitForAnimationToEnd` imports as it and it exports as that.
- `open <url>` on Android sends the VIEW intent with `am start -W` and
  reports `handled_by` — the activity that took the URL — plus the launch
  report; a URL that opened the system chooser warns `url_opened_chooser`,
  one nothing took `url_not_handled`. iOS answers `handled_by: "unknown"`
  (`simctl openurl` cannot say).
- `session start` reports what `--install` and `--launch` did: `installed`
  and `launched` (the app id with the iOS `pid`, or the Android `mode`,
  `component` and `am start -W` report).
- A single-file `flow run` result carries `runs: [<its run>]`, the list a
  directory run has, next to its existing keys.
- Every match `ui find` reports carries the `index` that selects it — with
  `--all` and without (null for an off-screen match no index reaches).
- `ui wait --settled` (and a flow's settle result) reports `dump_ms`, the
  slowest dump, and `stable_ms`, how long the last tree held still.
- iOS nodes carry `long_clickable` (always false: accessibility has no
  long-press trait) and `checkable` (true for toggles), so a node's key set
  is identical on both platforms again; Android nodes carry UI Automator's
  `long-clickable` and `checkable`.
- `simulator clipboard get` reads the iOS pasteboard (`simctl pbpaste`) and
  verifies it; `simulator push send` takes `app_id` from the session when it
  is left out (`app_id_source`); `capabilities` lists `simulator.keyboard`.
- Android `logs tail` and `logs follow --source device` report the
  `filter` a `--package` used (`uid`, `pid`, `none`), with
  `log_filter_degraded` when it is not the uid and
  `process_name_not_filtered` for a `com.x:remote` process name; iOS
  `logs tail` names the `executable` its predicate matched.
- `network status` reports `target_flow_count`, `unattributed_flow_count`,
  `recent_user_agents` and a `reason`, and raises `persistent_mocks_active`
  as `network start` and `doctor` do.
- `processes` lists session-owned background processes (`background`: the
  iOS log-stream writer, `canvas serve` pairs, idb companions Autonom's own
  idb calls started) and `group_remnants`; `cleanup` reports
  `skipped_unverified`, `group_remnants`, `skipped_group_remnants` with
  `unverified_skipped` / `group_remnant_left_running` warnings; `doctor`
  names every group remnant with its inspection hint.
- `session stop` reaps every registry row the session owns
  (`session_processes` in `teardown`, `process_teardown` on the record) and
  names an idb companion it did not start (`companion_left_running`) or a
  group remnant (`group_remnant_left_running`) instead of killing it.
- `session outputs` lists `metrics/` and `recordings/` artifacts
  (`followable`, `directory` for an Instruments `.trace`) with an `open` /
  `xdg-open` hint where the host has one.
- `flow create` names what it cannot compile instead of dropping it:
  `launch_args_not_compilable` (a `session launch`'s `--activity`/`--arg`/
  `--setenv`), `clear_state_not_compilable_on_ios`, `uninstall_not_compilable`,
  `open_url_not_compilable` (no scheme) and `open_url_not_recoverable`.
- `metrics snapshot` on Android adds `cpu_sampling` (how the CPU figure was
  obtained) and a `cpu_stale` warning; `metrics frames capture` warns
  `no_frames` at the top level for a window of zero HWUI frames.
- **Mobile Canvas scrcpy transport (Android)** — `canvas serve --transport
  scrcpy`, and `auto` whenever a scrcpy-server 4.1 is found, runs one
  scrcpy-server per Canvas through an `adb forward` and forwards its H.264
  packets byte for byte over the authenticated `/ws/video` WebSocket: no
  ffmpeg, no transcoding. The page decodes them with WebCodecs (codec string
  from the SPS, low-latency mode, newest frame only) and falls back to the
  multipart picture, with its reason, when the browser cannot. A late joiner
  starts at the last key frame; a client more than 4 MiB behind skips to the
  next one while the others keep streaming. `/ws/control` carries real-time
  touch (down, move, up and cancel, held fingers, up to 10 pointers,
  Ctrl/Alt-drag pinch), wheel and trackpad scroll on both axes, key down/up
  with Android meta state, Unicode text, paste, a device clipboard fetch, and
  Back, Home, App switch, Power, Volume, Wake, Notifications, Quick settings,
  Collapse and Rotate. Input after a paste through the device clipboard waits
  for the device's acknowledgement (at most 1 s) and 200 ms more, so a burst
  of text and pastes lands in the order it was sent. Keys of non-US layouts, of the AltGr and
  macOS Option layers and after a dead key type their own characters (input
  methods beyond dead keys are not covered). Rotation keeps input mapped; a
  device server that dies is restarted (1 s doubling to 10 s) while pages stay
  connected, and pages reconnect by themselves. A finger or key left down by a
  rotation or by a server that died is lifted, so none stays pressed on
  Android. The server starts with the first WebSocket client and stops 15 s
  after the last one leaves; stopping the Canvas ends it and removes the
  `adb forward`. `/status` gains `fallback_reason` and a `scrcpy` block
  (version, source, session state, size, packets, bytes, key frames,
  restarts, client counts, `journal_pending`, `journal_dropped`), and
  `window.autonomCanvas` exposes the page's counters and a `send()` for
  agents that drive the page. The WebSocket server and the scrcpy wire format
  are written by hand; the encoders are tested against scrcpy's own
  serialization test bytes.
- **scrcpy-server discovery** — `--scrcpy-server PATH` (with
  `--scrcpy-version` when the file name does not say), then
  `AUTONOM_SCRCPY_SERVER`, `SCRCPY_SERVER_PATH`, then the server an installed
  `scrcpy` ships beside itself. The first source that is set decides, and a
  broken one is reported rather than skipped. Only 4.1 is used, and Autonom
  never downloads a server. `canvas serve` gains `--scrcpy-server`,
  `--scrcpy-version`, `--max-size` and `--bit-rate`; `--transport scrcpy`
  without a usable server fails naming `canvas.scrcpy` and `brew install
  scrcpy`. `doctor` reports `optional_tools.scrcpy` (never part of
  `--strict`) and lists the two variables among its overrides;
  `scripts/bootstrap.sh` reports scrcpy as an optional tool and installs it
  with `--install --with-scrcpy`.
- **Journal for streamed input** — the Canvas bridge gains a `record`
  operation that journals an action the Canvas already performed and never
  actuates. The scrcpy path writes exactly one entry per completed gesture,
  wheel burst, key press, text entry, paste, system button and handoff change
  (`ui gesture|scroll|key|text|paste|system|control`, with its origin and
  `transport: scrcpy`), rebuilt from an allowlist so clipboard and sensitive
  text never reach it. Pointers and keys the Canvas lifts on a pause, a
  takeover, a disconnect or a server restart are journaled like released
  ones.
- `tests/live/canvas_scrcpy_live.mjs`, a live acceptance and benchmark run
  against one explicit `--serial` (cases `picture`, `bench`, `tabs`,
  `restart`, `input`, `journal`, one JSON report each in `--evidence-dir`),
  in its own temporary `AUTONOM_HOME` and never run by the gate.

### Changed
- `run_checks.sh` runs the unit suite through `tests/run_parallel.py`: one
  module per process (the slowest split per TestCase class), twice-CPU-count
  workers (`AUTONOM_TEST_JOBS`), a scratch
  `AUTONOM_HOME` each, the environment guards around every module, and the
  `tty_guard` refusing stdin in every worker. One pass replaces the serial
  suite plus its second `tty_guard` pass: about 70 s instead of 14 minutes
  on a 10-core Mac. `AUTONOM_SKIP_TTY_GUARD` is gone; CI job timeouts drop
  from 35 to 15 minutes.
- CI: pull requests run one Linux job on the minimum supported Python 3.11
  (including the `tty_guard` pass); the macOS job runs Python 3.14 on pushes to
  `main` only and skips `tty_guard` (`AUTONOM_SKIP_TTY_GUARD=1`). Local
  `run_checks.sh` is unchanged.
- `session start` refuses with the new `session_already_active` (naming the
  live session and hinting `session stop`) instead of silently replacing
  the current session and orphaning its proxy and recorders.
- `simulator` controls whitelist their actions: an unknown action (or a
  text-size category that does not exist) is `invalid_simulator_action` with
  `valid_actions`, refused before any device command. It used to surface as
  `flow_command_invalid`.
- `verified` is true only after a read-back that matches. Controls with
  nothing to read back (iOS battery, Android network and biometric, iOS
  status-bar pin, …) now answer `verified: false` with the reason in
  `verification`; an Android API without `cmd clipboard` refuses
  `clipboard set` with `unsupported_capability` (`reason:
  clipboard_unsupported`, `supported: false`) instead of a verified set.
- Android `permissions reset` revokes only the named package's granted
  runtime permissions (`pm revoke`); it used to run `pm reset-permissions`,
  which takes no package and reset every app on the device.
- `flow fmt --write` refuses with the new `comments_would_be_lost` when the
  canonical output would drop comments, leaving the file unchanged, unless
  `--drop-comments` is passed. In a directory nothing is written when any
  file would lose comments.
- `flow check` is stricter: a negative `timeoutMs`/`delayMs`/`durationMs`/
  `maxSwipes`/`chars`, a latitude or longitude out of range, an empty
  selector string, and a regex that does not compile are positioned errors
  (`timeoutMs: 0` stays legal). A UTF-8 byte-order mark is accepted.
- `flow export --format maestro` writes `launchApp resume` as
  `stopApp: false`, quotes every scalar YAML would misread, escapes an exact
  selector so it stays exact through repeated import/export, keeps regex
  selectors free of extra wrappers, refuses an `inputText` timeout and a
  postcondition instead of dropping them, and collects every refusal with its
  line rather than stopping at the first.
- `flow import` (Maestro) accepts `launchApp` `stopApp` (`true` is a fresh
  launch, `false` is `resume`) and maps text patterns back to the plainest
  Flow mode: an escape-only pattern (`Sign\ in`) becomes exact text,
  `(?i)literal` becomes `caseInsensitiveExact`, and `.*literal.*` becomes
  `contains`, so import-export-import is stable.
- `flow run --dry-run` answers top-level `status: "planned"` where it used
  to answer `"passed"` — a script that checked `status == "passed"` on a dry
  run must check `"planned"` now. The `planned` list numbers steps exactly as
  the executor does at runtime (pre-order across `runFlow` children and
  hooks), so an index read off it is a valid `--until-step`; an entry after a
  `retry`, a `while:` repeat, or a `when:` runFlow carries `index_exact:
  false` because its runtime index can shift. `--until-step` now trims the
  plan too.
- `teach approve` counts only replays whose recorded `flow_sha256` (and
  every `subflow_sha256`) equals the current bytes; `--minimum-runs` must be
  at least 1. A legacy manifest without a hash is counted by the Teach
  ledger's hash, else only when the file has not been modified since that
  run started; only those mtime-bound replays are listed as
  `legacy_unhashed` in the receipt, with a `legacy_unhashed_replays`
  warning.
- The idb stale-companion retry happens at most once and only for
  "Connection refused" — a failure that never delivered the action — and,
  when the companion registry is readable, only if pruning actually removed
  an entry. A reset connection is never retried, so a tap or typed string
  is never sent twice.
- `doctor --strict` that fails now answers `ok: false` with
  `strict_failures`, not only exit 1; a probe that times out on a slow
  device is a warning naming the target instead of an aborted doctor.
- Boolean flags (`--clickable`, `--enabled`, …) accept only
  true/false/1/0/yes/no; a selector plus coordinates on `ui tap`, or half a
  coordinate pair, is a `usage_error`.
- `network mock add` without `--url`/`--match` is `selector_required`; a
  body that looks like JSON must parse, `--status` must be 100..599, and a
  `--header` without `Name: value` is refused.
- On iOS `network attach` adds `attach_state: manual` next to the unchanged
  `attached: "unknown"`; the manual steps now describe the host Mac's system
  proxy, since the Simulator has no Wi-Fi proxy pane.
  `network status` keeps its `attached` value types.
- A malformed regular expression anywhere (`logs tail --grep`,
  `ui find --mode regex`) is `invalid_value` before the device is touched;
  `logs follow --grep` keeps answering `backend_failed` for compatibility.
- Flags that have no effect on the target platform (`--activity` on iOS,
  iOS-only launch flags or `session clear --strategy` on Android,
  `--log-stream` on Android) produce a `flag_ignored_on_platform` warning
  instead of being dropped silently.
- `simulator status-bar override` on Android now enters demo mode explicitly
  and accepts `battery`, `plugged`, `wifi`, `wifi_level`, `mobile`,
  `mobile_level`, `datatype`, and `notifications` alongside `hhmm`; an unknown
  key is refused before anything is broadcast.
- The fake `simctl` now moves a device to `Shutdown` on `shutdown`, so a
  shutdown/write/boot sequence is proven by the device list.
- **`ui tap` and `ui find` resolve on-screen matches first.** Duplicates and
  `--index` are counted among the matches that can be on screen — exactly
  as a flow counts — and every match only when none is; `ui find` without
  `--all` answers the node `ui tap` would act on. On iOS an off-screen copy
  (Flutter's scroll cache) had made a selector unique on Android
  `ambiguous_selector`, and `--index` picked another node than the flow did.
- `ui find --all` lists on-screen matches first (tree order), then the
  off-screen ones, so position *i* is what `--index i` selects.
- **Android resume launch uses `am start -W`** on the launcher activity with
  the launcher's own intent flags (`0x10200000`) instead of `monkey`, whose
  freeze/thaw reset a pinned orientation; `monkey` remains only for a
  package without a resolvable launcher activity. `session launch` reports
  `component` and the `launch` report; an explicit `--activity` resume waits
  too (`-W`).
- **`network status` counts only the target's traffic.** A flow is evidence
  only when its client is the target: an Android emulator guest flow proves
  attachment (`target_flows`); loopback flows are unattributed and the
  device's proxy setting, read back, decides (`device_setting`,
  `device_proxy_cleared_externally`, `setting_unreadable` when adb cannot
  read it). On iOS host and Simulator traffic are indistinguishable, so it
  stays `"unknown"` (`host_traffic_indistinguishable`). `recent_flows` is no
  longer answered: a host `curl` through the proxy used to read as attached.
- iOS `network attach` answers `mode: "manual"`, matching `attach_state`
  (it said `"automated"` next to `attach_state: manual`); `mechanism` names
  what was automated. It also warns `flutter_proxy_hook_required`: `dart:io`
  ignores the proxy environment unless the app sets `HttpClient.findProxy`.
- `report suite --last N` covers the latest run of each of the N most
  recently run flows: a re-run replaces its flow's earlier run instead of
  pushing another flow out of the window. `report history --last` still
  counts runs.
- `flow export --format maestro` exports a tap's `timeoutMs` (`tapOn`,
  `longPressOn`, `doubleTapOn`) as `extendedWaitUntil: {visible: <selector>,
  timeout: N}` followed by the tap (`optional` carried over) instead of
  refusing it; on import an optional pair of that shape folds back into the
  tap's `timeoutMs`, and any other optional wait refuses.
- `flow check` / `flow list` report the `platforms` a flow can really run
  on: `requires.platform`, else both minus iOS when a step that runs there
  is Android-only (`back`, `clearState`, `setOrientation`,
  `launchApp.clearState`, a `KEYCODE_*` key); iOS `flow run` refuses the same
  steps at pre-flight from the same table.
- `flow run <dir>` skips a flow whose platforms exclude the target (declared
  for the other platform, or an Android-only step on iOS) with a
  `flow_skipped_for_platform` warning (`skipped_flows`) instead of aborting
  the suite at that flow's pre-flight refusal; a suite of which nothing can
  run is `flow_no_flows_found`. A flow refused for anything else, such as a
  missing capability, still stops the suite.
- Android `logs tail --package` / `logs follow --source device --package`
  narrow by the app's uid on API 31+ (its lifecycle lines included, earlier
  processes of the app too), by the running pid on older APIs, else to lines
  naming it; an unknown package is `app_not_installed` instead of a
  screenful of other apps' lines.
- `cleanup` kills a registered process only after `ps` confirms it still
  runs the recorded command (`pid_reused`, `unverified_skipped` otherwise),
  and by default only when it is orphaned (its session stopped, its
  supervisor died, or it has no owner and no intact artifacts directory);
  `cleanup --all` also stops the `background` category. A process group
  whose recorded leader is gone is never signalled (`group_remnant`): its id
  may belong to someone else by now.
- `metrics snapshot` on Android measures CPU now — two `/proc` reads 0.5 s
  apart, on the one-core scale (a process busy on two cores reads 200) —
  and uses `dumpsys cpuinfo`, whose window may have closed minutes earlier,
  only as the fallback.
- `metrics frames capture` drops gfxinfo's `percentile_*_ms` and
  `janky_percent` for a window of zero frames (they were the histogram's top
  bucket and a meaningless 0%).
- `metrics trace --preset simpleperf` records `--app <id>` with
  `cpu-cycles` when `simpleperf list hw` lists it and `cpu-clock` otherwise
  (an emulator has no PMU); `metrics list-presets` marks `hitches`
  `unsupported_on_simulator`, and the iOS frames refusal points at a physical
  device, `flutter-summary` and `time-profiler` instead of `hitches`.
- `simulator push send` without `app_id` (and no session app) or a payload
  is `invalid_value` (was `flow_command_invalid`); Android `simulator
  clipboard get` is `unsupported_on_platform` (was `invalid_simulator_action`).
- `logs follow` ends with eof reason `max_lines` when a final unterminated
  fragment reaches `--max-lines` (was `stream_ended`).
- Registry rows drop detail keys whose value is null and store a `command`
  redacted.
- `canvas serve --transport auto` on Android prefers scrcpy, then
  `screenrecord` + ffmpeg, then `screencap`, and names the reason for a
  fallback at startup and in `/status`. `--fps` is passed on only when given:
  the multipart default stays 15, and scrcpy streams at up to 60 fps unless
  capped.
- All multipart pages of one Canvas (screenrecord + ffmpeg, screencap, iOS
  screenshots) share one capture loop instead of each starting its own, and
  a page more than 2 MiB behind skips frames instead of buffering them.
- A reloaded Canvas page resumes its session from its cookie, through a
  same-origin fetch only.
- `scripts/bootstrap.sh` refuses an argument it does not know (exit 2).
- Bump the library and both plugin manifests to `0.31.0`, so plugin caches
  keyed by version pick up everything in this release.
- The Mobile Canvas skill's description and its Codex listing describe the
  live scrcpy video and real-time control instead of an MJPEG bridge, and the
  skill says how to stop the Canvas.

### Removed
- The `android-smoke` workflow and `scripts/ci/android_smoke.sh`. It booted
  an API-30 emulator on every push to main only to tap a row in the
  preinstalled Settings app, touched no app under test and no network path,
  and never checked that the tap changed the screen. CI now runs no device;
  device runs stay manual and are judged by before/after artifacts.

### Fixed
- A process that has exited but whose parent has not reaped it yet (a
  zombie) no longer counts as alive: stopping one returns at once instead of
  waiting out the 5 s termination timeout and SIGKILLing a corpse; `logs
  follow` no longer tails the file of such a dead writer, and `doctor` reports
  it as a stale background pid.
- The iOS bounded log writer installs its stop handler before it starts
  `log stream`: a stop that landed in between used to end the writer and
  leave `log stream` running with nobody to stop it.
- `simulator status-bar pin` validates every key and value before the
  first device command (a bad `hhmm` no longer enters demo mode, a bad signal
  level no longer sets the battery); a live pin after a demo pin leaves demo
  mode first so the clock ticks again; the signal is pinned with an explicit
  RSSI after the profile; battery is verified by the exact level line.
- `status-bar clear` restores the battery and signal state recorded before
  the first pin (a pre-existing battery override survives) instead of
  resetting state the pin never owned; the snapshots live under
  `$AUTONOM_HOME/simulator-state/<target>.json`.
- A live Android `status-bar pin` no longer drifts between captures. On a
  real API 36 emulator the cellular bars fell to none and the battery icon
  vanished within 80 s of the pin (and with no pin at all), so no one-shot
  command holds the bar; the pin now records its values and
  `autonom screenshot`, flow `takeScreenshot`, and flow step/failure
  evidence re-send them right before the frame, then wait 400 ms for
  SystemUI to redraw. The screenshot payload and the flow event carry
  additive `repinned` and `repinned_controls`; nothing is re-sent after
  `clear`, over a demo-mode pin, or on iOS, whose simctl overrides persist.
- On iOS `status-bar pin` refuses the Android-only keys `mode`, `hhmm`,
  `wifi`, `mobile`, and `notifications` with a hint naming the iOS
  equivalent, rather than forwarding them to simctl.
- Repeated `simulator keyboard pin` calls merge only keys not yet recorded,
  so `reset` restores the state before the first pin; a locale pin keeps the
  region in `AppleLanguages`. `reboot=true` confirms the device reached
  `Shutdown` before writing and refuses Booting or Shutting Down states.
- `simulator biometric match|nonmatch` posts the Face ID notification on
  Face ID devices and the Touch ID one otherwise (both when the device type
  does not say, reported as `biometry`); `text-size` and `appearance` are read back on
  both platforms; the iOS clipboard is read back.
- Android launches work on an AVD without a hardware keyboard: the `monkey`
  fallback passes `--pct-syskeys 0`, and an `am start` that prints `Error:`
  with exit status 0 is a failure, not a launch.
- `session uninstall` reports a failed uninstall as `ok: false` on both
  platforms.
- `file pull` of a missing Android file no longer writes run-as's complaint
  as the file, `file ls` of a missing directory is not a listing, an unknown
  package is `app_not_installed` rather than `app_not_debuggable`, and an iOS
  pull of a directory is a typed error instead of a traceback.
- An unknown Android permission is `invalid_value` with a hint naming the
  `android.permission.*` form, and an undeclared one points at the manifest,
  instead of `backend_failed` with a Java trace.
- A focused non-editable node (a button) no longer counts as a verified
  `ui type` target on Android.
- `ui tree --max-nodes` reports `truncated` exactly when nodes were cut, for
  dumps and live trees.
- `logs follow --source device` on Android starts at the device's now
  (`logcat -T`) unless `--from-start`; the session's only device-log stream
  is the default source.
- `journal --session-id` works without `--follow`, `shots show` accepts the
  relative path `shots list` prints, and `shots list --max` must be >= 0.
- `--idb-host/--idb-port` and `AUTONOM_IDB_COMPANION` add `--companion
  host:port` to every idb call.
- The repair brief points at the step that ended the run — never a retry
  attempt a later attempt recovered, never a failing cleanup hook — names
  the file that contains it (`flow`, with `root_flow` for the replay),
  computes `--until-step` as the last completed step that is not an
  enclosing block, maps every selector field (`visibleText` as `--text` and
  `--desc`), keeps `--mode regex`, and carries `--secret NAME` /
  `--env NAME=<value>` placeholders; its advice matches the codes flows
  actually produce.
- Flow `inputText` with `timeoutMs: 0` checks focus once, and an incomplete
  dump while the field's screen comes up is polled again instead of
  aborting.
- Directory suites skip App Skill overlays (`.autonom/apps/**`) and files
  that declare another schema, so a workspace with a promoted skill no
  longer fails with `flow_parse_error`; a `runFlow` cycle names the step
  that closes it.
- Teach compile counts a closing assertion's selector toward confidence and
  gives zero confidence (needs review) when nothing was proven.
- An output path that cannot be written (`screenshot --out`, `network
  export`, `report export`, `file pull --out` onto a directory) is
  `output_not_writable` before the device is touched; a missing `--dump`
  file carries an error code; a usage error's hint is the usage of the verb
  that rejected the flag; Python older than 3.11 is named in a
  `tool_missing` envelope.
- `record start --name` cannot escape the session directory; `note add`
  refuses empty text; `metrics series` needs a positive count and interval;
  canvas values are validated before anything starts.
- `network start` keeps the library's warnings (`proxy_already_running`
  when a live proxy has a different port or body setting) and no longer
  claims full-body capture when the proxy it reused does not capture
  bodies.
- The tour's teardown is `try/finally` (session stopped, `--shutdown`
  honoured on failure and interrupt), an explicit target that is not
  running is an envelope rather than a traceback, and its tests never reach
  the host's real simctl, adb, or idb.
- The built-in tours pass on current targets. On Android 16 (API 36) a
  Settings sub-screen's title is the collapsing toolbar's description, not a
  text row, so the Android tour asserts the "Network & internet" and
  "Internet" titles by `description`; the iOS 26.5 Simulator Settings home
  has no Wi-Fi or Bluetooth rows, so the iOS tour recognises the home by
  `Accessibility` (Wi-Fi and Bluetooth still count where they are shown)
  and opens General by its `com.apple.settings.general` id.
- `doctor` keeps only version fields of the companion's output.
- `session stop` succeeds when the session's emulator or simulator no longer
  exists: the session is cleared with a `stale_target` warning (and
  `stale_target: true`), and the device-side proxy restore is skipped rather
  than failed. The `session_already_active` refusal names the session's
  target, says `autonom session stop` clears it even if that target is gone,
  and carries `stale_target: true` when it is.
- `devices shutdown` (and the tour's shutdown) restores a status-bar and
  animations pin before `emu kill`. Emulators quickboot from the snapshot the
  kill saves, so a pinned device came back with the battery override, hidden
  notification icons, and zeroed animation scales, while the shutdown had
  already dropped the record that could undo them. A record taken on the
  same emulator (AVD name and boot id match) is now restored exactly as
  `status-bar clear` and `animations reset` would, status bar first, and
  reported in the additive `pins_restored` key; a record from another AVD or
  boot sends nothing (`stale_pin_dropped` warning), and a failed restore
  warns `pin_restore_failed` — either way the emulator is killed and the
  record dropped.
- Docs: on API 36 emulators SystemUI may not draw the battery glyph although
  `dumpsys battery` reports the pinned level; on Xcode 27 with AXe carrying
  input, `doctor` shows `ios_hid.ready: false` with `ios_hid.axe_ready` and
  `ios_ui.ready` true, and agents are told to read those instead of giving up.
- **Off-screen iOS nodes.** iOS lists Flutter's scroll cache at a 0x0 frame:
  `ui tap` tapped the screen's origin and reported success, `assertVisible`
  passed on such a node, and `scrollUntilVisible` "found" one without
  scrolling. They are now marked `visible: false` (live trees and `--dump`
  alike), `ui tap` refuses them with the new `element_offscreen` (nothing is
  dispatched), and flows select on-screen nodes only; a flow that still meets
  `element_offscreen` classifies it as a test failure. The repair brief ranks
  and indexes on-screen candidates, so its confirming `ui find` resolves to
  the suggested node.
- Selectors normalise whitespace (`exact`, `contains`, both case modes): iOS
  joins a merged Flutter label with a newline, Android with a space, and one
  selector matched on one platform only. `regex` sees the raw value.
- `ui type` names the focused editable field (Android reported window focus
  on a FrameLayout ahead of the focused EditText), and `--interactable` no
  longer keeps Flutter's focusable-only static labels while keeping
  long-press-only and checkable controls.
- `ui wait --settled` never starts a snapshot it cannot finish before
  `--timeout-ms`, and one slow first dump no longer ends the wait early.
- iOS toggles report `checked` from `AXValue` (`1`/`0`, `on`/`off`), so
  `assertChecked` can pass on iOS, and count as clickable.
- **iOS logs of a Flutter app.** The predicate looked for the bundle id's
  last component while every Flutter app's process is `Runner`, so
  `logs tail --package` returned only `log show`'s trailer. `logs tail`,
  `logs follow` (live and from the session's file) and `--log-stream` now
  match the installed app (its executable, narrowed by binary UUID: see
  the next entry), so another Flutter app on the same simulator never
  leaks in; the `log` tool's banner and trailer are never returned as log
  lines.
- **iOS logs after a reinstall.** The unified log keeps reporting the
  container path at which it first saw a binary's UUID, so once the same
  build had been uninstalled and installed again (`session clear --strategy
  reinstall`, or by hand) every record named a deleted directory and the
  installed-bundle-path predicate matched none of them: on WoolBox
  `--log-stream` wrote 0 bytes and `logs tail --package` returned 0 lines
  while the app ran. The `log` predicate now names the app's executable,
  and its records are kept on the client by the image UUID each one
  carries (`processImageUUID` / `senderImageUUID`) against the installed
  binary's Mach-O LC_UUIDs — read by a bounded stdlib parser, thin or
  universal — so another Flutter `Runner` is still kept out. `logs tail`
  narrows `log show` output the same way, and the session records the
  UUIDs (`log_stream_image_uuids`) so its stream file stays readable once
  the app is gone or replaced. `logs follow` narrows the same way live,
  from the current session's file (with the UUIDs it recorded) and in a
  `--session-id` replay — which matches the session's own app by the
  identity its stream recorded, and any other `--package` by its installed
  binary. `logs tail` and the follow's `eof` line report the `image_uuids`
  they matched on. The stream file itself keeps every same-named app's raw
  records (a build installed while it runs must not be lost), so a read of
  it without `--package` — `logs follow` by default, `--source log_stream`,
  `--path logs/stream.ndjson` (or a hard link to it), `logs tail` — is
  narrowed to the session's app, by the identity its stream recorded; the
  simulator is asked only when that install is gone, belongs to another
  app, or was never recorded, for at most 5 s. Any other `--path` is read
  raw.
- A session's log-stream writer under a non-ASCII `AUTONOM_HOME` is
  recognised again: `ps` ran under the caller's locale, and with
  `LC_ALL=C` macOS prints `café` as `cafM-CM-)`, so the writer read as a
  stranger, was left running, and lost its registry row. `ps` now always
  runs under a UTF-8 locale. `session stop` keeps a log-stream row whose
  own signature still matches the live process (or that `ps` could not
  render), and a row that records only the bare stream path no longer
  lets `cleanup` or `session stop` signal a `tail -f` of that file.
- The session's iOS log stream survives the device moving under it:
  `simulator keyboard pin` with `reboot=true` and `session clear --strategy
  reinstall` restart it (`log_stream_restarted`, `log_stream_pid`), or warn
  `log_stream_stopped` when it cannot be restarted.
- The bounded log writer can no longer lose the previous rotation when
  `session stop` lands mid-rotation.
- No pid read back from a session file is signalled unverified any more. The
  log-stream restart after a reboot never signals the old writer (the
  shutdown already ended it) and after a reinstall stops it only once its
  command line is the bounded writer of the session's stream file — the
  writer's own script line plus the file as a whole argument followed by
  its cap, so `tail -f <file>` or a writer of `<file>.bak` never qualifies;
  a recorded pid that now runs something else gets a new stream started
  instead of being taken for the writer. `session stop` checks the
  log-stream writer and the screen recorder the same way (`recordVideo`,
  the UDID and the path; `screenrecord`, the device file and `-s <serial>`
  as adjacent whole arguments, so `R58M123` never claims `R58M123ABCD`'s
  recorder), and so do `record stop` and `record start`; a pid `ps` cannot
  vouch for is skipped with an `unverified_skipped` warning. An unrelated
  process whose pid had been reused used to receive SIGTERM, then SIGKILL.
  A registry row whose signature is empty (`""`, `[]`) is never signalled
  either: it used to be killed unchecked, as if it had none.
- An `idb_companion` registry row records the simulator's UDID next to the
  binary, so a pid reused by another simulator's companion is never taken
  for it.
- Android `network attach` refuses with `backend_failed` when adb cannot
  read the current global proxy; adb's error text used to be saved as the
  previous proxy and written back to the device by `detach`.
- `ui wait --settled` and flow `waitForSettled` no longer claim "the tree was
  still changing" when it was not: with one ~2 s Android dump in a 3 s
  budget the warning says settling could not be confirmed, how long a dump
  took, and how far to raise the timeout; a tree that changed and then held
  still for less than the quiet window is said to have done exactly that.
  Each case has one hint. Same code (`screen_not_settled`) and exit status.
- iOS `crash list` reports a listing idb could not produce (its companion
  was lost) as an error instead of `count: 0`; `permissions` checks the
  service against this Xcode's `simctl help privacy`; the iOS status bar,
  battery and biometric enrollment are read back for `verified`; simctl
  probes never raise.
- iOS `simulator clipboard get` with no usable `xcrun` is `simctl_not_found`
  like every other control (was `backend_failed`).
- Android `battery set` under a live status-bar pin updates the pin, so the
  next capture no longer undoes it.
- The iOS log-stream writer, `canvas serve` (now supervised in its own
  process group, node child and all) and the idb companions Autonom's idb
  calls start are in the process registry, so `processes`, `cleanup --all`
  and `session stop` can find them. Ending the CLI running `canvas serve`
  with SIGTERM, SIGHUP or Ctrl-C takes node and its children down; after a
  SIGKILL, which cannot be caught, the node child is listed as an orphan and
  `cleanup` reaps its process group.
- `canvas serve`'s supervisor signals the child's process group before
  reaping the child: a reaped leader frees its pid, and with it the group
  id, which a new process group could take before the signal was sent.
- Command-line redaction in the registry is linear: an unanchored URL
  pattern made a 400 KB argv take minutes, on every idb call once companion
  discovery ran there; discovery also redacts only a bounded head of each
  `ps` line.
- `flow create` reads journal argv as the parser does: `open <url> --serial
  S` compiles to `openLink: <url>`, `session launch` to `launchApp: {resume:
  true}`, `session launch --fresh` to the fresh `launchApp`; `flow check`
  refuses an `openLink` URL without a scheme.
- `simpleperf` failed on emulators with the default `cpu-cycles` event (no
  PMU).
- The fake emulator and fake adb write their state atomically and read it
  patiently, and the endless iOS log-stream test waits on progress instead
  of a timer — the two CI flakes of the hardening pass.
- The Canvas detected device H.264 by looking for `--output-format` in
  `screenrecord --help`, which Android 16 (API 36) no longer lists although
  `--output-format=h264` still works, so those targets fell back to
  screenshots. It now runs `screenrecord --output-format=h264 --time-limit 1`
  for at most 5 s and looks for an H.264 SPS.
- The Canvas bridge journaled into the current session whatever its target,
  so a Canvas on one device wrote its actions into the journal of a session
  on another. It now journals only when the current session is on the
  Canvas target (same `target_id`, and same platform); otherwise the action
  still runs and nothing is journaled.

### Security
- A live status-bar pin (and the animation/status-bar snapshot `clear` and
  `reset` restore) is bound to the emulator it was taken on: its AVD name
  (`adb emu avd name`) and boot id (`/proc/sys/kernel/random/boot_id`). The
  record lives under the adb serial, which names a console port, so an
  emulator killed without a clear used to hand its pin to whatever emulator
  took the port next — possibly the user's own AVD — before every capture.
  Now a mismatched, legacy, or unverifiable record is dropped without a
  single pin command (`stale_pin_dropped: true` plus a warning on
  `screenshot`, flow capture events, `clear`, `reset`, and a new `pin`),
  `devices shutdown` and `devices boot` drop the serial's record outright
  (`pin_record_dropped` / `stale_pin_dropped`), and a pin on a device whose
  boot identity cannot be read warns `pin_not_device_bound` and is never
  replayed. iOS records need no guard: they are keyed by the simulator UDID.
- Text sent through the Android device shell — `ui type`, flow
  `inputText`, and `simulator clipboard set` — is single-quoted, so
  `; & | $ \` ( ) < > " '` arrive as literal characters and can never form a
  second shell command.
- The journal resolves each argv token against the real parser (canonical
  option, dest, and the spelling as typed) and writes, per token, the most
  masked of that parser-resolved scrub, the argv-only scan, and the previous
  release's exact rule, so it is never less masked than before. `ui type
  --sensitive` text, every abbreviation of a secret-bearing flag in both
  `--flag value` and `--flag=value` form, `--token`, and every
  `KEY=VALUE` option value (`--env`, `--setenv`, …, journaled as
  `KEY=<redacted>`) are masked wherever they appear; credentials inside
  ordinary values (`?token=`, `password=`) are content-scrubbed.
- Sensitive query and fragment values (`token`, `access_token`, `api_key`,
  `key`, `secret`, `password`, `auth`, `session`, `signature`, `code`, AWS
  signature keys, and similar; decoded, case-insensitive) and the password
  in `user:password@host` are redacted in captured URLs and referers before
  they are stored, and again in `network requests list/show` and HAR export.
- `teach approve` binds an approval to the flow's content: the receipt
  records `flow_sha256` and `subflow_sha256`, a changed `runFlow` child
  refuses with the new `flow_source_changed`, and `app-skill promote`
  refuses a flow whose bytes differ from its receipt. An edit that
  preserves the file's mtime no longer passes as a replay.
- A `flow run --env K=V` whose value reaches a `sensitive: true` slot
  (directly or through a `runFlow` `env:`) is treated as a secret: redacted
  from manifest, events, and reports, recorded under `secret_names`,
  reproduced as `--secret K`, and named in a `flow_env_value_sensitive`
  warning.
- Command lines stored in the process registry, and those `processes` and
  `doctor` discover, are redacted: values of secret-named options
  (`--token`, `--api-key`, `--password=`, `PASS=`), `name=value` pairs whose
  name looks secret, and URL userinfo (`scheme://<redacted>@host`). A
  `canvas serve --token` never appears in `processes` or `doctor`; the
  supervised canvas rows carry no command line at all.
- Every Canvas request and WebSocket upgrade must carry `Host:
  127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding guard). A WebSocket
  upgrade needs the session cookie plus its CSRF value in the `csrf` query
  and the Canvas `Origin`, or the access token; a foreign `Origin` is refused
  whatever it carries, because browsers apply no CORS to WebSockets. The
  page cannot be framed (`frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  framed loads refused), since another local port is the same site and
  shares the cookie.
- Control messages are validated strictly (finite numbers, ranges, enums, an
  Android keycode allowlist, at most 64 KiB), and an invalid one is answered
  without touching the device. Canvas memory stays bounded whatever a client
  sends: reading pauses while the device, the connection's replies or the
  journal are behind, a connection that keeps sending past 1 MiB of held
  input is closed with 1013, and a full journal queue drops and counts
  records (`journal_dropped`). A client that already holds the token or the
  cookie can still slow input or delay journaling; that is accepted, as it
  can drive the device anyway.
- On the scrcpy transport, Unicode text (anything outside printable ASCII)
  and every paste go through the device clipboard and replace its content.
  Clipboard text is never logged or journaled, and the HTTP `/text` endpoint
  keeps its ASCII-only rule.

## [0.30.2] - 2026-10-01

### Changed
- Bump the library and both plugin manifests to `0.30.2`.
- Tighten the wording of the `autonom`, `mobile-flow`, `mobile-screen`, and
  `mobile-session` skills: the evidence ladder asks agents to climb as far as
  the claim needs and name the rung reached, and notes about past behaviour
  ("used to", "no longer", "existing workflows are unchanged") now state the
  current behaviour only. The Android `session launch` note also says when
  `monkey` is still used.

## [0.30.1] - 2026-09-30

### Added
- The Autonom entry skill instructs agents to check for updates at the start
  of a new task. The setup skill documents comparing plugin and CLI versions,
  downloading updates
  from the configured source, refreshing Codex and Claude Code through their
  native commands, and verifying the installed files before a new agent session.

### Changed
- Bump the library and both plugin manifests to `0.30.1` so agent caches can
  distinguish the updated package with the compact app-and-orbit icon.
- Correct the setup skill's inventory to 24 skills and include update requests
  in its discovery description and UI prompt.

## [0.30.0] - 2026-08-28

Autonom now implements the end-to-end product blueprint: strict portable
flows, semantic provider preflight, teaching and reusable app skills,
continuous step evidence, immutable report bundles, deterministic replay,
campaign-level CI, and one Android/iOS Mobile Canvas control plane.

### Added
- **Report Model v2 and immutable bundles:** canonical run, case, attempt,
  action, artifact, environment, setup, and replay records; content-addressed
  blobs; integrity verification; separate mutable annotations; complete
  per-step screenshot, hierarchy, log, and request deltas.
- **First-class exports and gates:** Allure 3 result files, compact agent JSON,
  JUnit, CSV, metrics, explicit gate rules, retry/flaky history, supervised CI
  shards, deterministic pack/merge/finalize, and independent publication.
- **Teach and App Skills:** marked journal ranges compile into reviewed flows;
  promotion requires three clean replays and stores only the fixed portable
  `.autonom/apps/<app-id>` contract.
- **Provider and setup contracts:** immutable semantic capability snapshots,
  preflight before mutation, a recorded Setup Catalog, explicit side effects,
  postconditions, simulator controls, and typed unsupported combinations.
- **Unified Mobile Canvas:** authenticated browser bootstrap, HttpOnly session
  cookies, CSRF checks, persistent journaled input, Android H.264 streaming,
  screenshot fallback, iOS Simulator point/pixel mapping, and explicit
  human/agent/replay control ownership.
- **Portable replay and Runtime Map:** bundle-contained flow graphs, replay to
  a stable step or checkpoint, and a descriptive `runtime-map` alias for the
  observed-only Atlas graph.

### Changed
- The event stream uses the ordered `autonom.event/v1` envelope with stable
  identity, monotonic and wall clocks, origin, attempt linkage, and
  pre-persistence redaction.
- The HTML evidence report has keyboard navigation, filtering, stable step
  anchors, first-causal-failure links, setup/capability inspection, and clear
  separation between execution status and proof verdict.

## [0.29.0] - 2026-08-28

The evidence report now implements the step-level debugging loop promised by
the product roadmap.

### Added
- **Manifest v3 step records:** stable source/runtime IDs, source columns,
  redacted canonical arguments, start/end timestamps, matched accessibility
  target bounds, pre/postcondition fingerprints, checkpoint indexes, exact
  execution commands, and step-correlated scrubbed network previews.
- **Addressable evidence UI:** every step has a direct timeline anchor and
  panels for before/after screenshots, matched-target highlighting, UI
  hierarchy diff, device logs, and request/response previews. An unattached
  network capture is explicitly unavailable instead of looking like an empty
  request list.
- **Portable prefix replay:** `flow run --until-step N` reconstructs state
  from the flow start, stops after the selected runtime step with status
  `replayed`, and skips cleanup hooks so the state remains inspectable.
  `--evidence` and repeatable `--collect` flags control the replay bundle.
- **Local report controls:** `report serve` binds to loopback and adds a
  token-protected replay button to each recorded step. Browser input is
  restricted to an existing run and step; it cannot provide a command or
  arbitrary path.
- `checkpoint` now captures its configured screenshot and hierarchy evidence
  and is recorded as an addressable replay boundary.

### Changed
- Evidence mode `always` captures both sides of every step so screenshot and
  hierarchy comparisons have a real before/after pair.
- Network records include millisecond boundaries for honest step correlation;
  headers and bodies remain scrubbed before they reach disk or the report.
- Prefix replay runs are reported separately and do not fail a session suite.

## [0.28.5] - 2026-08-20

Codex review of PR #1: three remaining findings, each pinned by a regression.

### Fixed
- **`when.envEquals` skip reasons leaked `--secret` values.** A mismatched
  condition printed both sides verbatim into events, the manifest, HTML, and
  JUnit. The reason now names the variable only when either side is a secret
  or a `sensitive:` runtime value.
- **Recovered `retry` attempts failed CI JUnit.** The executor keeps every
  attempt in `manifest.steps`; `report export --format junit` (and the suite
  document) counted those retained failures. Superseded attempts are now
  `<skipped message="retried"/>` and do not increment `failures`.
- **`requires.capabilities` was accepted and ignored.** The schema now
  freezes the research vocabulary (`ui.accessibility`, `screenshots`,
  `logs`, `network.capture`); preflight raises `flow_requirements_unmet`
  before the first mutation when the session cannot provide a declared
  facility.

## [0.28.4] - 2026-08-17

Manifest v2 and the evidence-integrity fixes an adversarial validation of the
whole 0.28.x arc turned up. The manifest is the report protocol, and it was
missing most of what a real report needs.

### Added
- **Manifest `schema_version: 2`** (additive; v1 manifests still render):
  wall-clock `started_at_ms`/`finished_at_ms` on the run and every step, the
  `selector` actually used per step, block spans for `group`/`repeat`/`retry`/
  `runFlow` (`blocks`, with iteration and attempt ranges, kept out of `steps`
  so JUnit keeps counting the same cases), `depth`/`parent_index`/
  `retry_attempt`, flow metadata (`tags`, `properties`, `description`, `env`,
  `secret_names`, `converted_from`, `workspace_root`, `evidence_mode`), and
  `artifact_steps` — the authoritative artifact→step ledger.

### Fixed
- **Reports called deliberate skips failures.** An `optional: true` step keeps
  the error it tolerated, and both suite renderers printed that error in
  failure red regardless of the step's status — a skipped step looked like a
  defect on every page. The error block is now gated on `status: failed`;
  a skip shows its reason with the tolerated code as muted text.
- **A frame too large to inline silently disappeared** from the single-run
  report (`_MAX_INLINE_IMAGE`, 2 MB — an ordinary 1080×2400 emulator frame
  exceeds it): the report then read as "nothing was captured". Oversized
  frames now render a placeholder naming the file and its size.
- **The sensitive guardrail stopped at the single-run report.** The suite and
  per-flow pages never looked at `sensitive`, and their assets were written
  0644. Both now carry the ⚠ banner, and a sensitive run keeps the whole
  output tree owner-only.
- `suite.xml` was hard-coded 0600 while everything around it was 0644 — the
  one file CI actually consumes was the one CI could not read.
- `report suite --detailed` never pruned its output: `runs/` and `assets/`
  from an earlier, different run survived into the new site and read as
  current evidence. Both are rebuilt from scratch (inside `--out` only).
- **`flow export --format maestro` dropped arguments silently** — `optional`,
  `reason`, `label` and `eraseText.chars` vanished, directly contradicting the
  profile's "convert faithfully or refuse" contract. They now carry over
  (Maestro has equivalents for all of them), and a per-command `timeoutMs`,
  which Maestro cannot express, refuses with a pointer to `extendedWaitUntil`.
  The refusal hint no longer claims `retry`/`scrollUntilVisible` are
  Autonom-only — both are Maestro commands that this release imports.
- An `AutonomError` raised while **evaluating** a `runFlow when:` or a
  `repeat while:` clause produced no step outcome at all: it unwound past the
  timeline, so the manifest showed no failing step. The failure now lands on
  the step that owns the condition.
- **An aborted run left no evidence at all.** An infrastructure or
  flow-definition failure raised straight out of `run()`, so `_write_manifest`
  never executed: the one run a human most needs to inspect had no manifest and
  no report. The error is now recorded (status, `primary_error`), the manifest
  is written, and the envelope still reaches the CLI unchanged (exit 2).
- **`takeScreenshot` frames were invisible in the suite site.** They carry a
  user label, not a `step-N` name, so the renderer filed them under step 0 —
  a bucket no step ever has — copying megabytes to disk that no page
  referenced. Frames are now mapped through the manifest ledger; the executor
  records a step for every capture, including `takeScreenshot`, which
  previously emitted no evidence event at all.
- **A screenshot label could impersonate a step number.** `takeScreenshot:
  step-1-decoy` filed its frame under step 1 and rendered it beside an
  unrelated command. Filenames are no longer parsed for step numbers.
- `report suite --detailed` was a no-op: the multi-page site was built
  whenever `--screenshots` was not `none` (its default is `failed`), so the
  documented single-page default never existed. The flag now gates the site.
- A malformed `match: regex` no longer reads as a clean negative in
  assertions (`select_all` shared `no_matching_node` with "invalid regular
  expression"), and a bad regex inside a **relational anchor** raises the
  positioned envelope instead of a raw `re.error` traceback.

## [0.28.3] - 2026-08-17

Suite-level evidence: running 46 flows produced 46 separate reports and no
way to see the run as a whole. Found by dogfooding a real app.

### Added
- `autonom report suite [--session ID] [--last N] [--out DIR] [--open]` —
  one page over every flow run in the session: totals (flows / passed /
  failed / total step time), a failures-first list, then every flow as an
  expandable block with its steps, durations and reproduction command
  (failed flows expanded by default). Same containment rules as the
  per-run report: no external fetch, everything escaped. Screenshots stay
  in the per-run reports — inlining dozens of runs would produce a
  multi-hundred-megabyte page.
- The same command writes `suite.xml`, a single JUnit `<testsuites>`
  document with one `<testsuite>` per flow — the shape CI dashboards
  expect. Exits 1 when any flow failed, 0 otherwise.
- `report suite --relative-to DIR` strips that prefix from flow paths and
  reproduction commands, so a report committed to a repository carries
  repo-relative paths (`autonom flow run .autonom/flows/…`) instead of one
  machine's home directory — found while committing a real report.
- `report suite --detailed` writes a small static site instead of a single
  page: `index.html` linking to `runs/<run_id>.html` per flow, each step
  with its screenshots, the device-log window captured at the failure and a
  link to the hierarchy dump. `--screenshots none|failed|all` controls whose
  frames are copied into `assets/` (default `failed`) — a whole suite's
  frames are ~100 MB, which is why nothing is inlined as base64 here.

## [0.28.2] - 2026-08-17

Selector fidelity for Maestro import, found by running a real Maestro flow
against a real Flutter app: the imported file executed but matched nothing.

### Added
- New selector field **`visibleText`** — the label a user or screen reader
  sees, wherever the platform stored it (`text` on Android views, the
  accessibility label on Flutter/iOS). One cross-platform field for flows
  that must run on both, while `text`/`description` stay strict
  single-attribute matches.

### Fixed
- **Maestro's `text` now imports as `visibleText`, not `text`.** Upstream,
  `text` matches the union of text / hintText / accessibilityText
  (`Filters.kt`); importing it as our strict `text` attribute meant every
  Flutter and iOS flow converted cleanly and then matched nothing —
  precisely the "parses but means something else" failure the profile
  exists to prevent. Verified end to end: a hand-written Maestro flow that
  switches the app language now passes unchanged through `flow run`.
  `flow export` maps `visibleText` back to Maestro `text`.

## [0.28.1] - 2026-08-17

Maestro Core Profile v2, slice 2 of 4: engine-only commands — value
extraction without a script engine, bounded iteration, and composition
completion. No new device substrate; everything rides on the existing
selector engine and gesture paths.

### Added
- **Run-scope variables, no JS**: `copyTextFrom` (selector → node text,
  description fallback; empty read = new test-failure code
  `flow_copy_empty`), `setClipboard` (literal), both into `into: NAME` or
  the implicit `COPIED_TEXT`; `pasteText` types it. Host-side only — the OS
  clipboard is untouched (exactly Maestro's semantics). Precedence env <
  variable < secret; `sensitive: true` redacts like secret input.
  Pre-flight is order-aware: use-before-definition, a name colliding with
  env/secrets (new code `flow_var_conflict`), and `pasteText` with nothing
  copied refuse statically; definitions inside `repeat` or a `when:`-guarded
  `runFlow` do not escape, and cleanup hooks see only `onFlowStart`
  definitions.
- **Bounded `repeat`** (leaves the deferred list): mandatory `times` 1–25,
  `while:` (`visible`/`notVisible`) checked before each iteration, per-block
  `iterations`/`stop_reason` in events; composition does not nest inside;
  violations are the new `flow_repeat_invalid`. No `allowMutations` gate —
  declared iteration is not failure recovery.
- **Composition completion**: `runFlow` accepts inline `commands:`
  (anonymous subflow; parent frame visible, `env:` overlays); `swipe`
  accepts `from: <selector>` (anchored at the element's center, clamped);
  `scroll` (one upward swipe); `scrollUntilVisible.centerElement` (≤3
  corrective micro-swipes along the scroll axis); `tapOn.repeat`/`delayMs`
  (2–10 declared taps); selector relation `containsDescendants` (any-depth,
  leaves the deferred fields).
- **Import widened accordingly**: the clipboard trio, `scroll`, bounded
  `repeat` (unbounded/JS `while` refuses), inline `runFlow.commands`,
  `swipe.from`, `scrollUntilVisible.centerElement`, `tapOn` `repeat`/`delay`
  all convert from Maestro files; their `_UNSUPPORTED_HINTS` entries are
  gone.

### Fixed
Hardening from the slice's adversarial review, each pinned by a test:
- **Relational selector constraints were silently dropped in every
  assertion/condition path** (pre-existing since 0.20.1): `assertVisible`/
  `assertNotVisible`/`waitUntil`/`scrollUntilVisible` and `when:` conditions
  matched on plain fields only, so an impossible relation could produce a
  false PASS (and a true one a false FAIL). All matching now routes through
  one relations-aware selection (`flow/selectors.select_all`); in an
  assertion context an absent geometric anchor means "not present", never an
  error.
- A `runFlow` file reference nested inside `group` escaped both walkers —
  `flow check` skipped its existence/cycle/containment checks and `flow run`
  crashed with a raw KeyError; both walkers now descend into every nested
  command list.
- Variable-name conflicts are checked against every name declared anywhere
  in the flow graph (child header envs, `runFlow env:` overlays), so a
  runtime variable can never silently shadow a subflow's own env; an inline
  `runFlow env:` can no longer shadow a secret; `pasteText` honors an
  env-declared `COPIED_TEXT`; a pre-flight memo hit still contributes the
  subflow's definitions (valid flows were refused); cleanup-hook definitions
  no longer leak between failure-isolated cleanup steps.
- `scrollUntilVisible` + `centerElement` no longer crashes with a raw
  IndexError when relations filter everything out, and its ambiguity
  semantics are explicit: centering needs exactly one match
  (`ambiguous_selector` otherwise, including mid-centering).
- The run-level `sensitive` flag now sees `sensitive: true` in hooks, nested
  command lists, and subflows — not just top-level root steps.
- `_swipe_from`/`_center_node` recover from a mid-flow rotation
  (COORDINATE_SPACE_MISMATCH refresh-and-retry) like `_tap`/`_swipe` do.
- Export refuses the new inline `runFlow.commands`, `tapOn.repeat`, and
  `swipe.from` instead of crashing (KeyError) or silently dropping arguments;
  `retry` refuses `repeat` inside (it hid mutating children from the
  `allowMutations` scan).
- The canonical emitter wrote every `when`-kind argument as `when:` — a
  `repeat.while` would have round-tripped as `when` (caught by the new
  round-trip tests before it could ship).

## [0.28.0] - 2026-08-17

Maestro Core Profile v2, slice 1 of 4 (`docs/plans/PHASE_6_MAESTRO_COMPAT.md`):
import courtesy. A real-world Maestro file now runs through Autonom without a
separate conversion step, and the most common idioms that used to die as raw
parse errors import cleanly — while the native Flow v1 grammar stays exactly
as strict as before.

### Added
- The strict parser accepts single-line flow mappings
  (`tapOn: {text: X, index: 1}`) in an opt-in mode used only by the Maestro
  importer — flat, scalar values only, every malformed shape a positioned
  `flow_parse_error` (new reason slugs `nested_flow_mapping`,
  `unterminated_flow_mapping`). Hand-written Flow v1 files still refuse
  `{...}` with reason `flow_mapping`.
- `flow run|check|fmt|list` execute Maestro files directly: a flow file whose
  header has no `schema:` field is converted on the fly through the importer
  (decision D6) — same refusals as `flow import`, nested `runFlow` children
  convert too. Converted runs carry `converted_from: maestro` in
  `flow.run.started` and the run summary; `flow fmt` prints the canonical
  Flow v1 text as the migration path — and `flow fmt --write` never rewrites
  a Maestro source in place (the entry reports `converted_from` +
  `write_skipped`; conversion to a file is always the explicit
  `flow import --out`).
- Import profile widened: header `properties`/`onFlowStart`/`onFlowComplete`
  (`url` refuses — no web target); map forms of `inputText` (`text`),
  `eraseText` (`charactersToErase`), `openLink` (`link`), `takeScreenshot`
  (`path`); `scrollUntilVisible` (`element`/`direction`; time/speed tunables
  refuse toward `maxSwipes`); `retry` (`maxRetries`+1→`maxAttempts`, capped
  at 3 attempts total, mutating children get an explicit
  `allowMutations: true` because that is Maestro's semantics); Maestro's
  on-selector `label`/`optional` move to the command (`optional` on tap
  commands only, with a generated `reason:`; an optional assertion refuses);
  `label` imports on every mapped command.

### Fixed
Hardening from the slice's adversarial review, each pinned by a test:
- Non-integer values where the importer expects a number (`swipe.duration`,
  `extendedWaitUntil.timeout`, `eraseText`, selector `index`) refuse with a
  positioned `unsupported_flow_command` instead of an uncaught traceback —
  and so does every malformed value shape (`properties: oops`, `env: oops`,
  a list under `retry`/`swipe`/`tapOn`, a scalar under `extendedWaitUntil`),
  which previously crashed with AttributeError and, through the auto-detect
  loader, could kill a whole `flow check <dir>` sweep.
- Maestro's YAML 1.1 boolean spellings (`True`, `yes`, `on`, …) normalize on
  import for `optional`/`enabled`/`clearState`; unrecognized spellings
  refuse. Previously `optional: True` silently imported as *not* optional.
- Convertible-but-invalid constructs (nested retry, negative `maxRetries`,
  empty `commands`, state-only selectors, malformed env names) refuse at
  their position in the *source* file; a validation error escaping the
  canonical rebuild is reported as a conversion failure instead of
  presenting canonical-text coordinates as source positions.
- Flow-mapping values keep mid-word apostrophes plain (`{text: Don't
  allow}`) — a quote is a quote only at value start, as in YAML.
- `docs/FLOW.md` prose caught up with the shipped surface: relational
  selectors and `retry` are implemented (not "deferred"), and `optional`
  applies to all three tap commands, not `tapOn` alone.

## [0.27.3] - 2026-08-15

### Fixed
Findings of the closing adversarial review of 0.26.0–0.27.2, each pinned
by a regression test:

- iOS pid resolution dropped its `pgrep -f <bundle-id>` fallback: the
  CLI's own command line contains the bundle id (`--app-id …`), so a dead
  app could "resolve" to the autonom process itself and `metrics
  snapshot` would silently measure the wrong program. A missing pid is
  now always an `app_not_running` refusal.
- Same-second metrics artifacts no longer overwrite each other: all
  writers share one naming owner (`metrics/artifacts.py` — `{stamp}-
  {label}` order everywhere, 0600, `-2`/`-3` suffixes on collision), so
  `metrics series --interval 0` keeps every sample. A snapshot's raw
  dump is now `…-meminfo.raw.txt` so `metrics memory analyze` (glob
  `*-meminfo.txt`) can never fold snapshots into a capture-pack series.
- A consumer closing the NDJSON pipe (`… follow | head`) ends the stream
  cleanly (exit 0) instead of a BrokenPipeError traceback; stalled
  device calls (`dumpheap`, simpleperf, a wedged adb) fail as one
  `backend_failed` envelope instead of an uncaught TimeoutExpired.
- `follow` reads files in binary with exact byte offsets: text-mode
  `tell()` returns an opaque cookie mid-multibyte-character, which could
  misread rotation and replay the whole file; reads are also bounded
  (1 MiB slices) and `follow_poll` never sleeps past `--max-seconds`.
  `--grep` is now case-insensitive like `logs tail`/`journal --grep`;
  a process stream's final unterminated line is emitted, not dropped.
- iOS `logs follow --source device` honors `--session-id` (replays that
  session's recorded stream, refuses if none), applies `--package` when
  tailing a stream file, and falls back to live `log stream` when the
  recorded file's writer is dead — a stale file no longer shadows live
  logs.
- `autonom proof` shares `--env`/`--secret` handling with `flow run`: a
  malformed `--env` is a hard `flow_command_invalid` refusal instead of
  a silently dropped override that could pass a diff under the wrong
  config. `parse_cpuinfo` can no longer credit `com.example.app` with
  `com.example.app.dev`'s CPU line. `metrics memory capture` warns when
  gfxinfo fails instead of silently omitting the artifact; `metrics
  memory analyze` without a session hints at `--dir` (the flag it
  actually has); Flutter frame-key preference is deterministic; `metrics
  frames reset` joined the bare-host sweep.

## [0.27.2] - 2026-08-15

### Added
- Metrics depth (research Phase 4 §2.4–2.6), closing the metrics phase:
  `metrics memory capture` writes the Android evidence pack (metadata,
  meminfo, proc status, gfxinfo, optional HPROF with `--no-hprof` for
  non-debuggable builds; the remote dump is always cleaned up) and
  `metrics memory analyze` runs the series math over captured meminfo
  files. `metrics memory warn` posts the simulated memory-warning
  notification on the iOS Simulator (best-effort, and says so).
- `metrics frames reset|capture` wraps gfxinfo with a best-effort summary
  (unknown API shapes stay honest: raw artifact + `parsed: false`);
  `metrics frames flutter-summary` is the CLI twin of the
  flutter-performance-audit script, pinned by an equivalence test.
- `metrics trace --preset simpleperf|gfxinfo-flow|allocations|
  time-profiler|leaks|hitches` records heavy profiles with an explicit
  duration into session `metrics/` — missing tools are `tool_missing` with
  an install hint, platform mismatches `preset_unavailable`, profiler
  failures `trace_failed` with the stderr tail. xctrace argv is fake-
  tested; real-Xcode recording remains a manual checklist item (⚠️ in
  CAPABILITIES).
- Skills now lead with the CLI (`android-memory-leaks`,
  `android-runtime-performance`, `flutter-performance-audit`,
  `flutter-memory-leaks`, `ios-debugger-agent`, `mobile-session`,
  `autonom`); the standalone scripts remain for hosts without a session.
- `docs/plans/PHASE_4_METRICS.md` renumbered to the shipped 0.27.0–0.27.2
  lanes (DEC-015 amended): the 0.16–0.19 reservation lapsed unused.

## [0.27.1] - 2026-08-15

### Added
- Metrics foundation (research Phase 4 §2.2–2.3, §2.7). `metrics snapshot`
  answers "how is the app right now": Android reads `dumpsys meminfo` +
  `/proc/<pid>/status` + best-effort `cpuinfo`; iOS measures the Simulator
  process **on the host** (`ps` RSS/CPU + data-container size) and carries
  `metric_semantics` + `limitations` saying exactly that — the two are
  never comparable 1:1. Partial data prefers `ok: true` with `warnings[]`.
- `metrics series` takes N spaced snapshots (or reads them back with
  `--from-dir`) and reports first/last/delta/slope per metric with
  `directional_growth_leads` — the algorithm is the proven one from the
  android-memory-leaks skill script, and equivalence tests pin the two to
  the same fixtures. The interpretation line is contract: a lead is never
  called a leak.
- `metrics list-presets` reports which heavy profilers this host can run;
  `doctor` gains a `metrics` capability block (optional profilers never
  fail `--strict`). Snapshot artifacts land under session `metrics/`
  (0600), raw meminfo text beside the JSON.
- Process identity helper: pid via `pidof -s` (Android) / `launchctl list`
  + `pgrep` (iOS Simulator); failures carry `sources_tried`. New stable
  error codes: `app_not_running`, `tool_missing`, `preset_unavailable`,
  `trace_failed`.

## [0.27.0] - 2026-08-15

### Added
- Live session observation (research Phase 4 §2L). `session outputs`
  catalogs every followable session file — registered `streams[]` plus a
  conventional scan of `output/`, `logs/`, `network/` — with `abs_path` and
  a copy-pasteable `shell_hint` (`tail -f …`) for a human's second terminal.
- `logs follow` streams a session file (`--path`, `--source output:<name>`,
  or a stream id) or the device log (`--source device`) as NDJSON lines,
  always bounded by `--max-seconds` / `--max-lines`, with `--grep`,
  `--from-start`, and rotation-aware tailing. Files are confined to the
  session artifacts dir (`path_forbidden`).
- `network requests follow` polls the flow store and emits only new flows
  as NDJSON; `network requests list --since-id` pages from a cursor (an
  unknown cursor returns everything plus a `since_id_not_found` warning,
  never a silent empty). `journal --follow` tails the session timeline.
- Session records register their long-lived writers: the iOS `--log-stream`
  file and the mitm flow store appear in `session.json` `streams[]`; older
  sessions fall back to the directory scan.
- New stable error codes: `stream_not_found`, `path_forbidden`. Every
  follow ends with one `{"kind": "eof", "reason": …}` line — the NDJSON
  streaming exception is documented in COMPATIBILITY.md alongside
  `flow run --events`.

## [0.26.0] - 2026-08-15

### Added
- Local PR Proof: `autonom proof --base <ref>` reads the git diff, selects
  the smallest sufficient flow suite deterministically (changed flow files,
  `properties.covers` globs, `pull-request` tags), runs it against the
  active session, and writes `proof.json` + a one-screen `proof.md`.
  Verdicts are fixed and never upgraded: pass / fail / not_covered
  (uncovered changed files listed by name; exit 1) / blocked /
  inconclusive (exit 2).


## [0.25.0] - 2026-08-15

### Added
- Atlas-lite: a local observed-only application graph. Screen fingerprints
  are computed from snapshots the executor already holds (free) and ride in
  run events; `atlas update` folds runs and manual tap details into
  `~/.autonom/apps/<app-id>/atlas/graph.json` — screens keyed by a
  volatility-resistant structure hash (clocks, counters, list length, and
  the status bar do not move identity; real state changes become variants),
  transitions labeled by the triggering command with evidence references.
  `atlas show|coverage|paths|export|diff` query it; coverage explicitly
  reports the unknown as unknown.


## [0.24.0] - 2026-08-15

### Added
- Evidence bundle per flow run: `flows/<run_id>/manifest.json`
  (schema-versioned status, step records, artifact inventory, reproduction
  command), failure log windows beside the screenshots and hierarchy dumps,
  and run-scoped screenshot grouping under `shots/<run_id>/`.
- `autonom report build|open|export` — a fully self-contained HTML report
  (inline data: screenshots, restrictive CSP, everything escaped — UI text
  is hostile input) and JUnit XML for CI, rendered from the manifest alone
  so a report can be rebuilt from the same run forever.


## [0.23.0] - 2026-08-15

The research doc's first flagship workflow: Session → Flow.

### Added
- Instrumented `ui tap|type|find`: each action writes an owner-only detail
  record (proven selector, matched node, surrounding tree, typed text or —
  with the new `ui type --sensitive` — only its length) under
  `<session>/actions/`, linked from the journal via the `detail` key.
- `flow create --from-session <id|current>` compiles the journal + details
  into a validated canonical flow: proven selectors verbatim, explicit
  `--index` carried as explicit `index`, sensitive input as `${SECRET_n}`
  (never stored, credential-shaped fields auto-detected), the final
  verifying `ui find` as the closing assertion, coordinate taps and
  point swipes reported as warnings instead of approximated; the response
  includes a quality report and the exact replay command.
- End-to-end proof in tests: a fake-driver session records, compiles,
  passes `flow check`, and replays green with `--secret` — with the secret
  absent from every artifact.


## [0.22.0] - 2026-08-15

### Added
- Maestro Core Profile import/export (`flow import`, `flow export --format
  maestro`). Regex-by-default matching converts honestly (metacharacter-free
  patterns become `match: exact`; real patterns anchor as `^(?:...)$` regex);
  everything outside the profile — scripts, JS interpolation, point
  coordinates, random input — refuses with a positioned
  `unsupported_flow_command`, and an ambiguous conversion never produces a
  file that silently means something else. Imports validate end-to-end
  before they are written.


## [0.21.0] - 2026-08-15

Flow v1 surface completion; ten review-confirmed executor/language defects
fixed (see the commit "Fix ten defects the adversarial review...").

### Added
- Relational selectors: `above`, `below`, `leftOf`, `rightOf` (pure edge
  geometry against a provably unique anchor), `childOf` (ancestors),
  `containsChild` (direct children) — powered by an additive `parent` ref
  every compact-node snapshot now carries.
- `focused` selector/state field on both platforms; iOS `AXFocused` was
  misfiled under `focusable` and is now mapped correctly (iOS `focusable`
  reads false — the platform has no such concept).
- Commands: `longPressOn` (Android zero-distance swipe / idb `--duration`),
  `doubleTapOn`, `setOrientation` (Android `user_rotation`; refuses on iOS;
  invalidates the executor's cached screen size), `retry:` (explicit,
  max 3 attempts, `onlyOn` code filter, mutations demand
  `allowMutations: true`, no nesting/runFlow inside, every attempt in the
  journal and events), `group:` (labeled boundary events).
- CLI: `ui tap --duration MS` long-presses via the same adapters.


## [0.20.2] - 2026-08-15

Flow DSL v1, slice 3 of 3: composition, suites, packaging.

### Added
- `runFlow` execution: children run inline with the root `appId` inherited
  and their own env frame (child header env < runFlow env < `--env` <
  secrets); child hooks do not run; the graph was already statically
  contained and cycle-checked.
- Hooks: `onFlowStart` aborts the run when it fails; `onFlowComplete` runs
  after pass and fail with each command isolated — failures are reported as
  `hook_failures` and never mask the primary outcome; failure evidence is
  captured before cleanup.
- `when:` conditions on runFlow (platform / visible / notVisible /
  envEquals, AND semantics); a false condition skips with the reason.
- Tag-filtered directory suites: `flow run <dir> --include-tag --exclude-tag`.
- Remaining commands: `scrollUntilVisible` (bounded, single-fire swipes),
  `assertEnabled`, `assertChecked`, `setLocation`, `setPermissions`,
  `addMedia`.
- Evidence policy honored: `mode: always` captures per step,
  `beforeMutation`/`afterAssertion` in custom mode, `minimal` disables
  automatic captures; unsupported collect kinds warn.
- The CI emulator smoke now ends with a real
  `flow run tests/fixtures/flows/settings_smoke.yaml`.
- New `mobile-flow` skill (the 24th) + routing/docs sweep.

## [0.20.1] - 2026-08-15

Flow DSL v1, slice 2 of 3: the executor.

### Added
- `autonom flow run <file>` — executes one flow against the active session:
  pre-flight against the resolved target before any mutation, polling
  assertions (`time.monotonic`, injectable clock, default 10 s / 500 ms),
  single-fire mutations (a tap polls only while zero nodes match, then the
  ambiguity-refusing selection applies once), `--env`/`--secret`
  (values never enter artifacts), `--events` NDJSON streaming, `--dry-run`.
- Failure taxonomy: exit `1` + `failure_class: test_failure` on stdout for
  assertion timeouts and selector misses; exit `2` with an additive
  `failure_class` field for definition/infrastructure errors.
- Per-run `flows/<run_id>/events.ndjson` (versioned envelope, chmod 600),
  slim `flow_step` journal lines, failure evidence (screenshot + hierarchy).
- `role` selector field in the shared engine and `--role` on `ui find|tap`
  (additive; the 0.4.0-compat path is regression-tested).
- `ui.tap`/`ui.swipe` accept a cached `screen=` size, removing the extra
  accessibility dump per action on iOS.
- Contract probes `flow_check`, `flow_run_pass`, `flow_run_test_failure`
  (golden extended by hand; the probe harness now reads stdout for exit-1
  reports).

### Fixed
- The journal choke point recorded handlers that returned nonzero as
  `ok: true`; it now journals the real outcome (`doctor --strict`,
  `flow fmt --check`, and `flow run` test failures were all affected).

## [0.20.0] - 2026-08-15

Flow DSL v1, slice 1 of 3: the language and its static tools
(`docs/plans/PHASE_5_FLOW_DSL.md`; versions 0.16–0.19 stay reserved for the
metrics phase).

### Added
- `scripts/autonom_lib/flow/` — a stdlib-only strict YAML-subset flow
  language: positioned parser (every rejected construct carries
  `file`/`line`/`column`/`reason`), typed command registry with mutating
  flags and failure classes, `runFlow` graph validation with
  symlink-resolved workspace containment and cycle refusal, and a
  deterministic canonical emitter.
- CLI verbs `flow check`, `flow fmt [--write|--check|--diff]`, `flow list`.
- `docs/FLOW.md` — the language reference, with its surface machine-checked
  against the registry by `tests/test_docs_flow_surface.py`.
- New `error_code` family `flow_*` (network capture's `flow_not_found` is
  untouched and reserved to it).
- Test corpus and suites: parser accept/reject table, canonical round-trip
  and idempotence properties, schema and validator rules, bare-host entries.

## [0.15.2] - 2026-08-15

Reliability and release-engineering foundation. No CLI verbs or flags changed.

### Added
- GitHub Actions: `checks` (full `run_checks.sh` on ubuntu + macOS with pinned
  Python/Node, mandatory shellcheck, actionlint), `android-smoke` (real API-30
  emulator driving the Settings app through the CLI on pushes to main), and
  `release` (tag-gated tarball + `SHA256SUMS` upload).
- `tests/env_isolation.py` — shared save-and-restore environment sandbox for
  tests, plus first/last alphabetical guard modules that fail the suite when
  any test mutates `os.environ` without restoring it.
- `docs/COMPATIBILITY.md` — the written CLI compatibility policy that the
  contract golden, docs-surface gate, and error-code rules already enforce.
- This changelog.

### Fixed
- Test isolation: `tests/test_devices_lifecycle.py` and two sites in
  `tests/test_network.py` deleted ambient `AUTONOM_HOME` (and, in one case,
  `CI`) from the process environment instead of restoring them; six more test
  call sites reached the operator's real `~/.autonom` when `AUTONOM_HOME` was
  unset. The suite now leaves the environment and the real machine store
  untouched, in any test order.
- `scripts/build_release.sh`: version is read via the library (the same
  resolver `validate_plugin.py` trusts) instead of a brittle grep; required
  bundle files are pre-flighted loudly instead of silently skipped; checks run
  before staging; `dist/SHA256SUMS` is emitted.
- `scripts/run_checks.sh` redirects `AUTONOM_HOME` to a scratch directory for
  the whole run, prunes `dist/` from shell sweeps, and can require shellcheck
  (`AUTONOM_REQUIRE_SHELLCHECK=1`, set by CI).

## [0.15.1] - 2026-08-07

Initial public baseline (commit `c63e32e`): session/device lifecycle, UI
tree/find/tap, screenshots with embedded provenance, logs and crash access,
consent-gated network interception with redaction and HAR export, process
registry, journal, and 23 portable agent skills.
