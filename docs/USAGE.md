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
Profile the exact scroll flow in Flutter profile mode on the physical device;
summarize with `autonom metrics frames flutter-summary <timings.json>`. On
Android wrap the flow in `autonom metrics frames reset` / `capture` (or
`metrics trace --preset gfxinfo-flow --duration 30`). Report UI/raster p90,
p99, worst and over-budget frames. Escalate to Perfetto or `metrics trace
--preset simpleperf` only if Android/plugin work is implicated.
```

## Investigate memory growth

```text
Take `autonom metrics snapshot --label baseline`, repeat the open/close flow
five times returning to the same idle state, then `metrics snapshot --label
after`. If the delta looks interesting, run `autonom metrics series --count 5
--interval 2` while repeating the flow and read directional_growth_leads.
A lead is a direction, not a leak: prove or reject a retained path (HPROF /
Instruments) before patching. Never compare Android total_pss_kb with iOS
rss_bytes — the payload's metric_semantics and limitations say why.
```

## First contact

```text
Run autonom tour, tell me what this machine can do, and if it offers a walk,
run it and give me the report directory and a step-by-step account.
```

## Prove a UI change with before/after screenshots

```text
Pin the status bar on the simulator, pin the keyboard to en-US, take a
screenshot of the settings screen, flip the dark-mode toggle, take another,
and report only the differences the app itself made. Then clear the status
bar and reset the keyboard.
```

## Capture a screen that never lands mid-animation

```text
On the emulator, pin the status bar and turn system animations off with
`autonom simulator animations pin`. Open the settings screen, run
`autonom ui wait --settled --timeout-ms 5000`, and only take the screenshot
once it reports settled: true. Afterwards reset animations and clear the
status bar, and tell me which values were verified by read-back.
```

## Diagnose a Flutter screen with an empty Android UI tree

Start an Android session with `--app-id`, then run `autonom ui tree`. If the
foreground app exposes only unlabelled containers, Autonom checks an unfiltered
dump. On an emulator with the built-in Accessibility Menu, it saves the secure
settings, enables the service, force-stops and relaunches that app, and rereads
the tree. Inspect `accessibility_recovery.recovered` in the result. Confirm the
new labels against a current screenshot: a disabled service can leave a stale
tree that does not follow navigation. `autonom ui accessibility status` shows
the managed state; `autonom ui accessibility reset` or `autonom session stop`
restores the saved settings. Use `ui tree --no-accessibility-recovery` to
inspect without changing the emulator, or `ui accessibility enable` to enable
the service explicitly. These changes apply only to an active Android emulator
session. A service can read screen content, so use it on an authorized test
emulator. A raw `adb exec-out uiautomator dump /dev/tty` comparison distinguishes
missing Android nodes from Autonom's meaningful-node filter.

## Repair a flow the app outgrew

```text
Run flows/checkout.yaml. If it fails with a test failure, follow the
`repair` block in order: replay the prefix with --until-step, dump the tree,
run the widened ui find, and check the ranked `candidates` with their
`ui find` commands. Tell me the label or identifier the button has now.
Propose the YAML edit; do not apply it without showing me the diff.
```

## Approve a flow for an App Skill

```text
Run `autonom teach approve .autonom/flows/login.yaml --run --minimum-runs 3
--secret TEST_PASSWORD`. If it refuses because the flow or a runFlow child
changed since its replays, tell me which file; do not edit it to get past the
check. Then promote it with `autonom app-skill promote <app-id> <flow>`.
```

## See what the app actually sent

```text
Start a consent-gated capture on the emulator, attach it, walk the checkout
flow, then list what went to the payments host, show the one that returned 500
in full, and export a HAR into the session. Report the request the app never
made separately from the ones that failed.
```

## Finish network capture safely

`autonom network stop` and Canvas Stop first detach the target, then stop the
proxy. Android detach restores the original legacy and canonical proxy settings,
refreshes Wi-Fi (briefly interrupting connectivity), and reads the settings back.
Repeated attach preserves the original restore snapshot, including when the
capture port changes.

If restoration or verification fails, the proxy stays running and the snapshot
is retained for retry. `session stop` also keeps the session open rather than
killing that proxy during process cleanup. Cleanup never blindly overwrites a
setting it did not write. If the device proxy was changed outside Autonom and no
longer points at the capture, that setting is left as it is, the attachment is
finished with a `device_proxy_changed_externally` warning, and the proxy stops.
While the device still points at the capture, stop refuses when the saved
original (legacy or canonical) is a local proxy that is no longer reachable,
names the capture itself, or was changed in a way Autonom did not write. The
refusal names the exact `adb ... settings put global http_proxy :0` repair;
after that repair the device no longer points at the capture, so a retry
finishes with the warning above. Attach refuses a device whose legacy or
canonical proxy already names the capture, since its original is unknown. A transparently routed emulator must be
shut down before its proxy can be stopped. Certificates are left unchanged.
`--session-id` or a target flag picks which session's device is restored; a
`network stop` or `network detach` aimed at a device other than the bound
session's is refused (`session_target_mismatch`) before any device write. The
Canvas Tools `network.stop` and `network.detach` act on the Canvas's own target.
`session stop` still reaps the session's processes when the proxy fails to stop
after a successful restore.

These guarantees apply to orderly cleanup. An abruptly killed proxy or host crash
still requires inspecting the device proxy with `autonom doctor` before using it.

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

## Drive the Simulator on Xcode 27

```text
Run autonom doctor. If `ios_hid` is not ready because idb_companion cannot
load SimulatorKit, tell me the fix it prints; if AXe is installed, confirm
that `ui tap` reports backend axe and that the tree still comes from idb.
Select iOS controls with --desc, not --text.
```

## Watch and drive the Simulator at 60 fps in the browser

```text
Start `autonom canvas serve --platform ios --target <udid>` and open the printed
URL. Confirm the Diagnostics panel says `transport: idb (webcodecs)` and the
caption shows about 60 fps while you scroll Settings; if it says screencap,
report `/status` `fallback_reason`. Drag a list with the mouse, press Home, then
stop the Canvas with SIGTERM and check that no idb_companion is left running.
```

## Mirror an Android device at a chosen size

```text
Start `autonom canvas serve --platform android --serial <serial>` and read the
`Stream size:` line it prints, or `/status` `scrcpy.max_size` and
`scrcpy.encoder`: an emulator (software H.264 encoder only) streams 2048 on
the long side, a phone with a hardware encoder streams native size, and 1280
means the encoders could not be read. For a pixel-exact picture on the
emulator start it again with `--max-size 0` (native, more host CPU); for a
busy Mac try `--max-size 1600`. Report the size and encoder with any fps
numbers.
```

## Change device state from the Canvas Tools drawer

```text
Start `autonom session start --serial <serial> --app-id <app id>` (on iOS
`--platform ios --udid <udid> --log-stream`), then `autonom canvas serve
--serial <serial>` (on iOS `--platform ios --udid <udid>`) for that same target
and open the URL. Press Tools in the toolbar. On App, revoke
the camera permission and set the location to a preset; on Android press
Read back and say whether the fix was delivered or only requested. On
Network, press Start capture, read the confirmation, confirm, Attach device,
use the app, open one request, press Mock this, set status 503 and add the
mock; check the app's error state, then remove the mock, Detach and Stop
capture. On Logs, filter to the app package and Warning level. Report each
step's result line and any error code and hint; never paste request bodies
or log text into the report.
```

## Work on several devices in one Canvas workspace

```text
Run `autonom canvas` (no target: a workspace Canvas that starts with no device
and keeps running until Ctrl+C or `autonom canvas stop`) and open the printed
URL. In the empty tile press Attach on `emulator-5580`, choose layout 2 and
attach the Simulator into the second tile. Each attached device gets its own
Autonom session automatically, or reuses the live session already running on
that target; `current.json` is never changed by the Canvas. Press + for a new
tab, and in its picker press "Move here" on the Simulator (a device lives in
one tab only). Use Ctrl+Alt+1..4 to focus a tile and Ctrl+Alt+Left/Right to
switch tabs; only the focused tile streams at full rate. From a terminal,
`autonom canvas list` shows the running Canvases and their tabs,
`autonom canvas attach --serial <serial> [--tab <id or name>]` and
`autonom canvas detach --serial <serial>` change the devices, and detaching
ends only the sessions the Canvas started. Report each device's session id and
whether it was reused.
```

Notes:
- A second `autonom canvas` for the same workspace prints the running one's URL
  (`already_running: true`) instead of starting another; `--workspace NAME`
  keeps several apart, and `--ephemeral` keeps a workspace's tabs out of the
  saved file.
- Tabs and the devices attached at stop are saved per workspace, so a restart
  brings the tabs back (`/c/<tab id>` links keep working) and attaches the
  devices that are running again.
- Screenshots and recordings from the Actions drawer go to
  `~/Downloads/Autonom/<device>/` (`--captures-dir DIR` or
  `AUTONOM_CAPTURES_DIR`); at 200 files or 2 GB per device the oldest are
  deleted and the gallery says so. Installing a build works only from folders
  named with `--install-root DIR` (repeatable); there is no upload.
- `--bootable avd:NAME[@PORT]` or `--bootable simulator:UDID` lets the picker
  boot a device; with `--shutdown-booted` the Canvas shuts down what it booted
  when it stops. Pass `--port 0` for a free port.

## Share a device between agents and a person

```text
Start one session per device: `autonom session start --serial emulator-5584
--label checkout`, then `autonom session start --serial emulator-5586
--alongside --label search`. `autonom session list` shows both live sessions,
their labels and which one is current. Before a long job on emulator-5584,
run `autonom control acquire --serial emulator-5584 --holder agent:checkout
--ttl 600` and pass the printed claim (`--control-holder agent:checkout
--control-generation <n>`, or the `env` it prints) to every command and flow
on that device; renew with `autonom control renew` before the TTL ends and
release with `autonom control release` when done. If a command answers
`control_held` or `control_generation_stale`, or a flow ends
`interrupted`, someone else has the device: report the holder from the
error, wait until `autonom control status --serial emulator-5584` reads
`free`, and only then run the flow again.
```

Notes:
- With no lease file a device works exactly as before: nobody needs to
  acquire anything. The lease only matters once someone holds it.
- In the Mobile Canvas a person's **first input** on a device nobody holds
  takes the lease automatically: unclaimed agent commands on that device are
  refused `control_held` (exit 2, nothing ran), and a running flow stops
  before its next device action with `status: "interrupted"`,
  `failure.failure_class: "control_lost"` (exit 1). The page then reads "You
  have control" with a **Release** button.
- A person sees "Agent '<label>' has control" while an agent holds the
  lease; input is off until they press **Take over**, which replaces the
  agent at once (its next action fails `control_generation_stale`). After a
  Take over the button reads **Hand back**.
- The person's lease ends on Hand back or Release, after `--control-idle`
  seconds without input (`autonom canvas serve --control-idle 300`; default
  120, 15..3600), or about 30 s after the page closes. Hand back never
  restarts an interrupted run.
- `control acquire --takeover` is refused for an agent holder
  (`takeover_requires: "human"`); only a person's Take over replaces a live
  holder. An agent lease expires after its TTL (default 60 s, 5..3600; a
  renew without `--ttl` keeps that TTL); a
  claimed flow that runs longer ends `interrupted` with
  `control_lease_expired` unless it is renewed in parallel.
- The lease is a coordination rule between cooperating processes of one
  user, not a security boundary. Any process of that user can call
  `control acquire --kind human --takeover`, or reuse a holder id and
  generation it read from a refusal or from `control status`. The lease
  files are private to the user (mode 0600 in a 0700 directory), so other
  users can neither read nor forge them. Do not rely on the lease to keep a
  misbehaving agent off a device.
- Reading never needs the lease: `ui tree`, `screenshot`, `record`, `logs`,
  `journal`, `session list`, `session stop` and `control status` always run.
  Host-side network work (`network start`, `network mock …`) is not gated
  either; `network attach|detach|stop` are.
- `--operation-id ID` names an action in the journal (`operation_id`); an
  admitted command's JSON gains `control: {generation, holder_id,
  operation_id}` once a lease exists or a claim was given.

## Tap one of several identical controls

```text
The catalog has several "Add" buttons. Run `autonom ui find --desc Add --mode
exact --all`: on-screen matches come first, each with the `index` that
selects it, and off-screen ones (visible: false, index null) last. Tap the
second visible one with `autonom ui tap --desc Add --mode exact --index 1`,
and if a tap answers element_offscreen, swipe it into view first instead of
tapping coordinates.
```

## Check which app took a deep link

```text
Run `autonom open myapp://order/42` on the emulator and read `handled_by`: it
names the activity that took the URL. If it is Chrome, or the warning says
url_opened_chooser, the app's intent filter or App Link verification is the
bug — report that, not a navigation failure inside the app.
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

## Pre-test a ticket before it goes to QA

```text
Pre-test ticket WB-7 (ticket.md) against main on emulator-5554. Read the ticket
and `autonom pretest context`, plan the checks before touching the device
(acceptance points, then edge cases from `autonom pretest kit`, then the selected
regression flows), begin and end each check, record findings with expected vs
actual and evidence refs, then pack and tell me the ticket verdict, what was not
covered, and the pack path. Do not tap anything that buys, pays or deletes.
```

The `ticket-pretest` skill carries this playbook for the agent: the order in
which to plan checks, how to run and end each one, how to write a finding
(expected vs actual, evidence refs), the verdict rules, the QA review, drafts,
repro and rerun, and the 18-case edge-case kit with the verbs that set up each
case and its platform limits (`references/edge-case-kit.md`).

The same pre-test by hand, inside a running session:

```bash
autonom session start --serial emulator-5554 --app-id com.example.app --launch
# the app runs before `pretest start`: the start takes the metrics baseline
autonom pretest start --ticket ticket.md --base main --ticket-ref WB-7 \
    --max-actions 200 --max-minutes 45        # or: --ticket - < ticket.md
autonom pretest context                       # changed files, hunks, selected flows, screen hints
autonom pretest kit                           # the 18 edge-case ids for --case

autonom pretest check add "Results appear after a search" --kind ticket
autonom pretest check add "Search offline" --kind edge --case offline
autonom pretest check add "A 500 shows Retry" --kind ticket --case server-error   # an acceptance point that is a kit case
autonom pretest check begin chk-1
autonom ui tap --text Search                  # counted against --max-actions
autonom screenshot                            # filed under the check (task chk-1)
autonom pretest check end chk-1 --verdict fail --note "500 not handled"
autonom pretest finding add --check chk-1 --severity major --title "Error swallowed" \
    --expected "an error message" --actual "an empty list" --evidence shot:1

autonom pretest flows                         # each selected flow becomes a regression check;
                                              # `flows` lists {flow, check_id, run_id, status, verdict}
autonom pretest status                        # budget used and left, open check, counts
autonom pretest health                        # crashes, error logs, failed requests since start
autonom pretest check list
autonom pretest finding list
autonom pretest pack                          # summary.md, index.html, integrity-checked pack
autonom report verify <pack>
```

In the pack, each check lists the requests captured in its window (method,
host, path, status, mocked, duration), each step lists the mocks active at
that moment, screenshots taken under a mock are marked as mocked data, and a
finding's steps to reproduce stop at its latest evidence. `index.html` opened
from disk fills the review commands with the pack's own directory. An ended
check cannot be ended again (`no_open_check`): add a finding instead.
`ui swipe --duration` is in seconds (`--duration 0.4`).

Then QA reviews the pack (no session needed except for `more-checks`):

```bash
autonom pretest review <pack> return-to-dev --finding f-1 --note "500 path missing"
autonom pretest review <pack> approve --dismiss f-2 --author anna
autonom pretest review <pack> more-checks --check "Rotate on results"   # starts a linked child pre-test
autonom pretest review-status <pack>
```

The `more-checks` child copies the pack's ticket and diff context, never its
safety rules: its app, budget, forbidden text and allowed apps come from your
session and the defaults (300 actions, 60 minutes, the default forbidden
list), or from `--app-id`, `--max-actions`, `--max-minutes`, `--forbid-text`
and `--allow-app` on the review. The output shows them under
`child.app_id` and `child.budget`.

Turn a finding into a regression flow, measure how often it reproduces, and
rerun it after the fix:

```bash
autonom pretest flow-draft f-1                # .autonom/flows/drafts/wb-7-f-1.yaml, tag draft, covers = changed files
autonom flow check .autonom/flows/drafts/wb-7-f-1.yaml
autonom pretest repro f-1 --times 3           # reproduced "1/3", stability intermittent, three run ids
autonom pretest pack
# ...the fix lands, the app is rebuilt...
autonom pretest rerun <pack> --check chk-2    # a linked child pre-test runs chk-2's draft again
autonom pretest pack                          # the child's summary shows chk-2 before fail, after pass
```

The draft is compiled from the journal of the finding's check, so drive checks
with selector-based taps (`ui tap --text ...`) and end them with a `ui find` on
the state you expect; read the draft's closing assertion before relying on it,
because a repro run that fails reproduces the finding and a passing run means it
is gone. `repro` records a result only when all N runs reached a verdict: a
broken run, a dead device or a spent budget stops it and nothing is recorded.
`repro` works on the active pre-test only (`--pack` is refused with
`pretest_finalized`); `autonom pretest flow-draft f-1 --pack <pack>` writes a
draft from a pack without changing it, and `rerun` without `--check` runs every
check of the pack that has a flow or draft. The child gets this session's app
and the default safety rules, never the pack's.

`autonom pretest stop` closes a pre-test without a pack, and
`autonom pretest list` shows the session's pre-tests. A tap on forbidden text,
an app outside `--allow-app` or a spent budget is refused before any input
reaches the device (`forbidden_action`, `app_not_allowed`, `budget_exhausted`);
the refusal is in the journal. Every journal entry, screenshot and saved tree made while a check
is open carries `pretest_id` and `check_id`.

### App profile and warm-up

An app profile tells the pre-test how to start the app. It is read from the
workspace first, `<repo>/.autonom/apps/<app-id>/profile.yaml`, then from the
home, `~/.autonom/apps/<app-id>/profile.yaml` (`$AUTONOM_HOME/apps/...` when set);
`autonom pretest context` shows which one was used under `profile` (`null`
when there is none). It is written in the same YAML subset as flow files:

```yaml
schema: autonom.pretest-profile/v1
login_flow: login.yaml                 # relative to this file's folder
warmup_flow: warmup.yaml
accounts:                              # environment variable NAMES, never values
  qa:                                  # names start with AUTONOM_ or PRETEST_
    user: PRETEST_QA_USER
    password: PRETEST_QA_PASSWORD
fixtures:
  product: Blue mug
flaky_screens: [Splash]
events:                                # rules for `autonom pretest events`
  - name: add_to_cart
    method: POST
    host: analytics.example.com        # or a glob: "*.example.com"
    path: "/v1/events*"                # glob over the path without its query
    body_key: event                    # dotted for nested JSON, or a form field
    body_value: add_to_cart
```

A profile never holds a secret: an inline password, token, bearer header, JWT
or URL with a password is refused with `profile_invalid` (the key path is
named, the value never), and `pretest start` then creates nothing. A value
written like a variable name (`PRETEST_QA_PASSWORD`) is read as a name.
`accounts` may only name environment variables whose names start with
`AUTONOM_` or `PRETEST_` (then upper case, digits and `_`); any other name
(`HOME`, `AWS_SECRET_ACCESS_KEY`, `QA_USER`) makes the profile invalid
(`profile_invalid`, the hint names the rule), and warm-up never reads any
other variable.

```bash
export PRETEST_QA_USER=anna PRETEST_QA_PASSWORD=...   # handed to the flows as secrets
autonom pretest warmup                        # login_flow, warmup_flow, Runtime Map update, walk
```

`autonom pretest warmup` runs `login_flow` and then `warmup_flow`, updates the
Runtime Map from the session (as `autonom atlas update` does), and reports each
screen hint of the diff as `reached` (the recorded selector taps of a known
path were replayed and the screen is on display), `unreplayable` (the path
holds a swipe, a key, a coordinate tap or a relational selector), `known` (in
the map but not reached; `reason` says why) or `unknown` (not in the map). It
never explores beyond recorded transitions. Its flow steps and taps count
against the budget, and it refuses while a check is open.

### Accessibility and analytics checks

```bash
autonom pretest check add "Cart is accessible" --kind a11y
autonom pretest check begin chk-3
autonom pretest a11y                          # unlabelled controls, small targets, duplicate labels
autonom pretest check end chk-3 --verdict pass

autonom network start && autonom network attach   # analytics needs captured traffic (consent)
autonom pretest check add "Cart sends add_to_cart" --kind analytics
autonom pretest check begin chk-4
autonom ui tap --text "Add to cart"
autonom pretest events                        # matched (request ids), missing, unexpected
autonom pretest check end chk-4 --verdict fail --note "add_to_cart not sent"
```

`autonom pretest a11y [--check ID]` saves the current tree under `trees/` and
writes one automatic finding (`severity: minor`, `confirmed: false`) per
control without a label, control smaller than 48×48 dp on Android or 44×44 pt
on iOS, and group of controls with the same label, each with the node bounds and
the saved tree as evidence. Without a readable screen density on Android the
size rule is skipped with a `density_unknown` warning. Contrast and reading
order are not checked.

`autonom pretest events [--check ID]` matches the profile's `events` rules
against the requests captured during the check (the open check, else the whole
pre-test). Each missing rule becomes an automatic finding; `unexpected` lists
requests to a rule's endpoint that match no rule, with request ids and body key
names. Body values are never copied into the output, findings or journal.
Without captured traffic the result says `state: unavailable` and no finding is
written. Automatic findings never change a verdict until QA confirms them.

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

## Recovering iOS UI and naming a session

See [UI recovery](IOS_UI_RECOVERY.md) for `--ui-backend auto|idb|xcuitest` (and how
it sits above `--ios-hid`), `--session-id`, measured `capabilities --probe`, and
repeatable Reader fixture flows.
