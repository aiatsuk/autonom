# UI recovery through Autonom

The CLI is the control boundary. `idb`, XCUITest, `simctl`, and `adb` are internal
backends; switching one does not require agents to leave Autonom or lose evidence.

## iOS

`--ui-backend auto` is the default. It uses idb first. When idb fails, returns no
meaningful elements, or has no valid application/window frame, an explicit app
session can fall back to the bundled XCUITest runner, and the result records why
in `fallback_reason` (`empty_accessibility_tree`, `invalid_accessibility_geometry`,
or the idb error code). Once selected, XCUITest continues serving that session.
`--ui-backend idb` and `--ui-backend xcuitest` select a backend explicitly (also
`AUTONOM_UI_BACKEND`); passing it to `session start` persists the choice.

The backend choice sits above the HID route. While idb serves the tree, input
still goes the way `--ios-hid auto|idb|axe` (`AUTONOM_IOS_HID`) decides: idb, or
AXe when idb's HID cannot load SimulatorKit (Xcode 27 with an old companion),
and input payloads say `backend: idb` or `backend: axe`. Once XCUITest serves
the session it carries the input too (`backend: xcuitest`), and the HID route is
not consulted. Replacing AXe with XCUITest was considered and rejected: AXe
keeps idb's tree and needs no app session.

The optional runner requires full Xcode, builds on first use, and caches its build
by source and Xcode version under `$AUTONOM_HOME/xcuitest/` (default
`~/.autonom/xcuitest/`). It builds and runs with Xcode 27 (deployment target
iOS 17). The generated Xcode project is included: XcodeGen is
only needed when editing `native/ios/project.yml`, not by users. No package manager,
third-party service, credentials, or network listener is required. The targetless
UI-test runner installs its own helper on the selected Simulator; it does not
install, launch, clear, or replace the app under test. App lifecycle stays explicit
through `session` commands. The first build/attach takes longer than subsequent
commands. The runner exits after five idle minutes and is stopped by `session stop`:
asked through its mailbox first and, if it does not exit, ended through its process
registry row (kind `xcuitest`, checked against its command line before any signal).

Supported XCUITest operations: tree/find, selector or coordinate tap, long press,
swipe, text, and Home. Other hardware keys still use idb. Each snapshot has fresh
references. Semantic taps re-resolve the observed type, identifier and label and
require one hittable match. A changed or ambiguous element is refused before input.

The Simulator-container mailbox has a per-run owner nonce and per-request ID.
Mutations are submitted once. A missing response returns `ui_action_uncertain`;
inspect the screen before another action. Never automatically repeat an uncertain
tap or submit it to another backend. Read failures may select another backend.
The runner log is private: XCTest may log typed values; do not paste it into reports.
It is deleted with the run's result bundles (below) and when a runner never gets ready.

Zero, negative, or missing dimensions are not a screen. Child element extents are
not a display size. With unusable idb geometry, Autonom can measure the selected
Simulator's device-type screen scale (`capabilities.plist` on Xcode 27, the
older `profile.plist` keys before) and validate it against a fresh PNG's pixel
dimensions and orientation. XCUITest uses the live SpringBoard frame in points.
If geometry cannot be established while the XCUITest runner serves the session,
`display_geometry_unavailable` is returned before dispatch; on the idb/AXe route
the input is sent unguarded, as before. The result states the geometry source
and units.

Screenshots remain independent of UI trees: `autonom screenshot` uses simctl,
preserves provenance, and keeps working when idb fails.

```bash
autonom session start --udid <udid> --app-id com.iatsuk.reader
autonom ui tree --session-id <id>
autonom ui tap --session-id <id> --resource-id notebook --mode exact
autonom screenshot --session-id <id> --label notebook
autonom capabilities --session-id <id> --probe
autonom journal --session-id <id>
autonom session stop --session-id <id>
```

`capabilities --probe` reads the UI; it never sends input just to test readiness.
On iOS, installed tools stay the baseline (a ready idb means
`ui.accessibility: available`, and `ui.input` follows idb's HID and AXe as
before). A UI read in the last minute replaces `ui.accessibility` with what was
measured, a degraded tree with its reason; while XCUITest serves the session,
`ui.input` is `available` after a runner input in the last minute and
`degraded` until then. Flows that require `ui.accessibility` or `ui.input` read
the UI once in preflight. None of this proves an action's intended outcome:
assert the resulting text or compare before/after evidence.

## Sessions and evidence

`--session-id` binds a command, its artifacts, proxy state and journal to one
session without switching the global current pointer; `current` names the current
one. Another target's flags are refused (`session_target_mismatch`). Stopped
sessions remain readable (`journal`, `shots`, `logs`, `report`, `session
show|outputs`) but refuse everything else (`session_stopped`), including
`report serve`, whose `/replay` drives the device; nothing rewrites a stopped
session's `session.json`, and nothing is appended to their journals: a command that names one is journaled in the current
session, if there is one, as before the flag existed. A command whose target flags
name another device than the current session's (without `--session-id`) is still
journaled in the current session. One session per
machine store still holds: `session start` refuses a second live session and never
takes `--session-id`. The new flags match only when spelled in full, so `--u` is
still `--udid` and `--se` still `--serial`. The runner has one owner per Simulator
and serializes requests. Its result bundles hold element labels and are not
evidence: a new runner first deletes the ones an ended runner of that Simulator
left, a runner that never gets ready deletes its own and the runner log, and
`session stop`, once the runner has exited, deletes this run's bundle, any older
ones and the runner log. A newer runner started concurrently and still alive keeps
its own bundle and the log it writes. Concurrent journal appends allocate sequence numbers
under a file lock. `session stop` is recorded after teardown even though the
current pointer has been removed.

Before its first teardown step, `session stop` writes the stopping mark on the
session record: `stopping_at` (when the first active stop began) and
`stopping_tokens`, one owner token per `session stop` still tearing down
(both `session.json` and the current pointer, atomically and under the
record's lock). From then on a runner request for that session is refused
with `session_stopped` while any stop holds the mark: when it starts, again
once it holds the Simulator's request lock, and once more right before a new
runner process would start, after the build (each check reads the session's
own `session.json`, since a finished stop has already removed the pointer). So
a command racing the stop never leaves a runner behind it. A stop that aborts
before the session is stopped (an accessibility restore that fails keeps the
pointer) withdraws only its own token: while another `session stop` is still
tearing down the mark stays and requests are still refused, and once no token
is left the mark is dropped and the session stays usable. A stop killed
outright leaves its token, and running `session stop` again finishes the job.
The mark stays on the stopped record beside `stopped_at`. A bare `stopping_at`
without `stopping_tokens` counts as one stop in progress; records written by
earlier versions simply have no mark. Other commands ignore it.

Results and flow events include the selected UI backend and fallback reason.
Screenshots and flow assertions supply outcome evidence; successful command exit
alone only means dispatch succeeded.

## Android

Normal launch resolves the launcher Activity and uses `am start -W` without clearing
the existing task (main already did this when the work was ported; its result
reports `component`, `mode` and the `-W` `launch` report). `--fresh` retains its
explicit clear-task behavior. When no launcher Activity can be resolved, monkey
remains a reported fallback. An explicit component is supported:

```bash
autonom session launch com.iatsuk.reader --session-id <id> \
  --activity me.yatsuk.reader.ReaderActivity
```

## Regression scenarios

`examples/flows/reader-offline-draft-ios.yaml` opens the fixture article, writes a
draft, stops and relaunches the process, and asserts the same text with screenshots.
Use an isolated Simulator and Reader's fixture-account SQLite library, never a
personal library. Fixture preparation belongs outside the UI flow and must not
create a network session. The flow deliberately appends text and checks containment
so it can be replayed without clearing app data.

`examples/flows/reader-google-cancel-ios.yaml` opens the system Google permission
prompt, asserts its controls, cancels it, and verifies return to Reader without
entering credentials. See [validation evidence](IOS_UI_RECOVERY_VALIDATION.md)
for measured results and the limits of these fixture scenarios.

Unit regressions (`tests/test_ui_recovery.py`) cover zero roots, valid child
windows, partial extents, rotated PNG geometry, unknown-screen refusal, empty-tree
fallback, uncertain single-dispatch input (also for `auto` following a persisted
runner observation), the XCUITest and idb/AXe composition, runner stop, the
stopping mark (a request during teardown, a stop that begins while the
runner builds, an aborted stop, two concurrent stops of which one aborts),
measured capabilities, session selection,
flag abbreviations, resolved Android launch and the live script's redaction
helpers.

## Live check

`tests/live/ios_ui_recovery_live.py --udid <UDID> --evidence-dir DIR
[--expect-xcode-major N]` runs the runner for real on one isolated simulator,
under a temporary `AUTONOM_HOME`: first a plain idb session (on the fresh
boot), then an explicit `xcuitest` session against Settings, an `auto` session
whose idb answers an empty tree (through a forwarding `AUTONOM_IDB` wrapper)
with one semantic tap on the General row, and one `xcuitest` session that
checks each runner input with its own pass/fail field: text entry (a fixed
non-secret string typed into the Settings search field must be the field's
value), long press (a 1 s press on that field must open the text edit menu),
swipe (rows absent before must appear and the General row must move up) and
Home (the next runner tree must no longer hold Settings). After every
`session stop` no runner `xcodebuild` may be left. `ok` also requires the
simulator to end in the state it started in and `xcodebuild -version` to
report the expected Xcode major version (default 27). It boots the simulator
only when it is shut down, and shuts it down again only then. It writes
`ios_recovery.json`; of the typed text only its length is recorded, and runner
logs are never copied. See [validation evidence](IOS_UI_RECOVERY_VALIDATION.md).
