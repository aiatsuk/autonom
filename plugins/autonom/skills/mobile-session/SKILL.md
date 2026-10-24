---
name: mobile-session
description: Own an explicit Android device or iOS simulator session for AI agents — list targets, start Autonom session artifacts, install/launch/force-stop/clear apps, and stop cleanly with evidence directories.
model: inherit
---

# Mobile Session (Android + iOS Simulator)

## Purpose

Give the agent a **single explicit target** and an artifact directory before UI,
log, or network work. Prefer this skill over ad-hoc `adb` or `simctl` when testing
or debugging.

One verb set covers both platforms. `--serial` is an Android alias of
`--target`.

## CLI entrypoint

```bash
python3 <autonom-root>/scripts/autonom.py devices          # Android + iOS in one list
python3 <autonom-root>/scripts/autonom.py doctor           # what this machine can actually do
```

Each device carries `running` (true when Booted / `device`), so you can tell a
live target from a cold one without knowing each platform's wording. The
Android listing also reports `avds` (bootable emulator images not yet started)
and `avd_profiles` — each AVD's hardware profile, screen size and density, and
API level read from its `config.ini` — so "the phone-sized emulator" is a
lookup, not a guess from the name. A running emulator names the `avd` it
booted from.

`doctor` lists every active `AUTONOM_*` override under `overrides` and warns
with `override_path_missing` when one points at a binary that does not exist —
the usual reason a tool reads as missing while `which` finds it.

### Boot and shut down a target

```bash
python3 <autonom-root>/scripts/autonom.py devices boot --avd Pixel_9      # start an Android emulator, wait for boot
python3 <autonom-root>/scripts/autonom.py devices boot --udid <UDID>      # boot an iOS simulator
python3 <autonom-root>/scripts/autonom.py devices shutdown --serial emulator-5554
python3 <autonom-root>/scripts/autonom.py devices shutdown --udid <UDID>
```

`boot --avd` waits for `sys.boot_completed` and returns the serial it came up
as (`--no-wait` to skip the wait). `session start` still boots a shutdown
simulator on its own, so `devices boot` is only needed to start an Android
emulator up front or to pre-warm a target. `shutdown` refuses any serial that
is not `emulator-<port>` — it never powers off physical hardware.

An emulator started by `devices boot` is registered as **harness-owned**:
`doctor` does not list it as an orphan while it runs, and `devices shutdown`
releases its registry entry, so no stale process warning follows.

### Android

```bash
python3 <autonom-root>/scripts/autonom.py session start --serial emulator-5554 --app-id com.example.app
python3 <autonom-root>/scripts/autonom.py session launch com.example.app            # resume where it was
python3 <autonom-root>/scripts/autonom.py session launch com.example.app --fresh    # launcher activity on a cleared task
python3 <autonom-root>/scripts/autonom.py session launch com.example.app --fresh --activity .MainActivity
python3 <autonom-root>/scripts/autonom.py session force-stop com.example.app
python3 <autonom-root>/scripts/autonom.py session clear com.example.app
python3 <autonom-root>/scripts/autonom.py session stop
```

### iOS Simulator

```bash
python3 <autonom-root>/scripts/autonom.py session start \
  --platform ios --target <UDID> \
  --install build/ios/iphonesimulator/Runner.app \
  --launch --app-id com.example.app --log-stream

python3 <autonom-root>/scripts/autonom.py session launch com.example.app --setenv FLAVOR=staging
python3 <autonom-root>/scripts/autonom.py session force-stop com.example.app
python3 <autonom-root>/scripts/autonom.py session stop
```

Target flags work before or after the subcommand. A shutdown simulator is booted
automatically and the response reports `"booted": true` when this call did it.
The response also says what `--install` and `--launch` did: `installed`
(true/false) and `launched` (`null`, or the app id with the iOS `pid` / the
Android `am start -W` report: `mode`, `component`, `launch`). An Android
`session launch` (resume) is the launcher icon's own `am start -W` intent, so
it keeps a pinned orientation; `monkey` is used only when no launcher activity
resolves.

`session start` refuses with `session_already_active` (naming the live
session's id and target) while a session is current — run `session stop`
first; it never replaces a session silently. If `--install` or `--launch`
fails, the new session is rolled back (`session_rolled_back` names it) so no
half-built session is left current. A flag that does nothing on the
platform (`--log-stream` or `--setenv` on Android, `--activity` on iOS) is
reported in a `flag_ignored_on_platform` warning, and `session uninstall`
reports a failed uninstall as `ok: false`.

Intent extras that start with `--` must be attached to the flag, or argparse
reads them as options: `session launch com.example.app --arg=--es --arg=key=value`.
Argument mistakes come back as `error_code: usage_error` with the usage line.

All commands print JSON. `session start` writes, **machine-globally** under
`~/.autonom/sessions/` (honouring `AUTONOM_HOME`) — not in the project, so the
active session is found from any directory, like mocks and per-app knowledge:

```text
~/.autonom/sessions/<session_id>/{shots,trees,logs,network,recordings,crashes,files,journal.ndjson,session.json}
~/.autonom/sessions/<session_id>/flows/<run_id>/events.ndjson   # per-step flow run evidence (mobile-flow)
~/.autonom/sessions/current.json
```

### Session journal — the full record of a run

Everything a session does is appended to `<session>/journal.ndjson`: every verb
(what ran, its scrubbed arguments, success, the artifact produced) and any note
the agent writes. Read it back for analytics or a handoff:

```bash
python3 <autonom-root>/scripts/autonom.py journal                     # whole timeline
python3 <autonom-root>/scripts/autonom.py journal --kind note         # only notes
python3 <autonom-root>/scripts/autonom.py journal --verb 'ui tap'     # only taps
python3 <autonom-root>/scripts/autonom.py note add "login screen renders; password field visible" --task login
python3 <autonom-root>/scripts/autonom.py note list --task login
python3 <autonom-root>/scripts/autonom.py journal --session-id <id>   # a finished session's timeline
```

The journal is secret-safe (typed text, secret-bearing flag values in any
spelling or abbreviation, and every `KEY=VALUE` option value are masked) and
best-effort (a journal error never fails your command). `ui tree` keeps a
sequenced file per capture under `trees/`, so the whole run's screens are kept,
not just the last.

## Clearing app data

| Platform | Behavior |
| --- | --- |
| Android | `pm clear` — full data reset |
| iOS default | uninstall + reinstall; needs the `.app` path, so start the session with `--install` |
| iOS `--strategy privacy` | `simctl privacy reset all` — **permissions only, app data survives** |

Without a recorded install path the verb fails with
`ios_clear_requires_install_path` rather than silently doing something weaker.

## Remote iOS targets

`idb` splits into a macOS companion and a client that can run elsewhere, so a
Linux orchestrator can drive a Mac:

```bash
export AUTONOM_IDB_COMPANION=mac-farm-01:10882
# or: --idb-host mac-farm-01 --idb-port 10882
```

## Rules

1. If more than one target is ready, **always** pass `--target` (or `--serial` / `--udid`).
   Ambiguity is an error listing the candidates, never a silent guess.
2. Do not print signing secrets, tokens, or `.env` values while installing/launching.
3. One session per investigation; stop when done so artifacts stay coherent.
   `autonom session outputs` lists every followable file in the session dir
   (device log stream, process output, network flows) with a `tail -f` hint
   for a human's second terminal, plus the `metrics/` and `recordings/`
   artifacts (`followable: false` for a binary, `directory: true` for an
   Instruments `.trace`, with an `open` hint where the host has one).
4. `session stop` tears down the log stream, recorder, and proxy best-effort,
   then every process the session registered (the iOS log-stream writer, a
   `canvas serve` pair, an idb companion its own idb calls started), and
   reports each action under `teardown` — it never fails because teardown
   failed. Every pid is signalled only once its registry row or its command
   line shows it is still that process (a pid in a session file can be days
   old); one `ps` cannot vouch for is skipped with `unverified_skipped`. A
   companion it did not start is named in a `companion_left_running`
   warning, never killed.
5. If a session died without stopping, `autonom doctor` lists orphaned processes,
   any device left pointing at a dead proxy, and any emulator still routed
   through another session's *live* proxy (`device_attached_to_foreign_proxy`).

## Failure codes worth knowing

| Code | Meaning |
| --- | --- |
| `ambiguous_target` | more than one ready target; pass `--target` |
| `session_already_active` | a session is current; `session stop` it before starting another |
| `canvas_not_found` / `canvas_ambiguous` | `canvas stop/attach/detach` found no live Canvas, or several; pass `--port` or `--workspace` (`autonom canvas list`) |
| `idb_required` | iOS `ui` verbs need idb; `screenshot`/`logs`/`open` still work |
| `ios_boot_failed` | simulator never reached `Booted`; try `xcrun simctl erase <udid>` |
| `no_active_session` | start one with `session start` |
| `usage_error` | argparse rejected the argv; `hint` carries the usage line |
| `app_not_debuggable` | `file ls`/`pull` need a debuggable build; release and system apps refuse run-as |
| `simulator_must_be_shutdown` | `simulator keyboard pin` needs a shut-down simulator; pass `--value reboot=true` or shut it down first |
| `simulator_data_not_found` | the simulator has no data directory yet — boot it once, or set `AUTONOM_CORESIMULATOR_DEVICES` |
| `control_held` | someone else (a person in the Canvas, another agent) holds the device's control lease; nothing ran — wait for `control status` to show it free |
| `control_generation_stale` | your lease was replaced (a person took over); acquire again, and never replay an input whose outcome you did not see |
| `control_lease_expired` | your lease ran out; renew before the expiry or acquire again |
| `control_not_held` | you claimed a lease you do not hold (or there is none); acquire it, or drop the claim |

## Backend recovery and explicit sessions

Keep device actions inside the Autonom CLI, including screenshots and recovery.
`--session-id <id>` binds one command to that session (its artifacts and journal)
without changing the current session. A stopped session stays readable (`journal`,
`shots`, `logs`, `report` but not `report serve`, `session show|outputs`) and refuses
everything else with `session_stopped`; another target's flags are refused with `session_target_mismatch`.
`session start` without `--alongside` still refuses a second one while the current
session is live. A Canvas workspace (`autonom canvas`) starts one session per device
it attaches with `session start --alongside --started-by canvas:<port>:<pid>`, or
reuses the live session already on that target; those sessions never become current,
and a command with `--serial X` / `--udid U` binds to the live session on that target,
so its journal lands next to the Canvas's. `session stop --serial X` stops the session
on X; detaching the device in the Canvas stops only sessions the Canvas started, and
`session stop` never stops a workspace Canvas (it has no session owner).
A stopped session's journal never grows: a command that names it is journaled in the
current session, if there is one.

On iOS, `--ui-backend auto` tries idb (its input still routed by `--ios-hid`, AXe
included) and falls back to the bundled XCUITest runner when idb's tree is empty, has
no usable root frame, or fails; the runner then serves both tree and input. An app
session with `--app-id` and full Xcode are required for the optional runner; its first
use builds and starts the helper. `--ui-backend xcuitest` selects it directly;
`--ui-backend idb` isolates idb diagnosis. The option on `session start` persists the
choice. `capabilities --probe` measures current UI readiness; without a recent read,
`ui.accessibility` describes installed tooling only.

For duplicate labels use the observed identifier or add `--role button --mode exact`.
A `stale_ui_element` refusal happened before input: refresh the tree and reselect.
A `ui_action_uncertain` result means input may have happened: inspect the screen
before doing anything else and never automatically repeat the mutation.
A `display_geometry_unavailable` refusal must not be worked around with guessed scale.
Screenshots remain `autonom screenshot` even when the tree is empty.

Android launcher failures can be handled with
`session launch <app> --activity <component>`; normal launch now resolves the
launcher Activity automatically.
Use before/after trees or screenshots to verify the actual result. Fallback choices
and flow steps remain in the session journal. See `docs/IOS_UI_RECOVERY.md` in the
Autonom repository for implementation details and the Reader regression flows.

## Several sessions and shared control

```bash
python3 <autonom-root>/scripts/autonom.py session start --serial emulator-5584 --label checkout
python3 <autonom-root>/scripts/autonom.py session list            # live sessions; --all adds stopped ones
python3 <autonom-root>/scripts/autonom.py control status --serial emulator-5584
python3 <autonom-root>/scripts/autonom.py control acquire --serial emulator-5584 --holder agent:checkout --ttl 300
python3 <autonom-root>/scripts/autonom.py --control-holder agent:checkout --control-generation 3 ui tap --serial emulator-5584 --desc Pay
python3 <autonom-root>/scripts/autonom.py control renew --serial emulator-5584 --holder agent:checkout --generation 3
python3 <autonom-root>/scripts/autonom.py control release --serial emulator-5584 --holder agent:checkout
```

- `session list` prints `{"ok", "sessions", "count"}`; each entry has `session_id`,
  `platform`, `target_id`, `label`, `started_at`, `stopped_at`, `started_by`,
  `current` and `live`. It never touches a device; target flags filter it. A
  record that cannot be read is skipped with a `session_unreadable` warning.
  `--label TEXT` (1-64 printable characters) names a session for people.
- **The control lease** says who may change a device right now: one file per
  target in the machine state, with a holder (`kind` `agent` or `human`, `id`,
  `label`), a generation that only grows, and an expiry. With no lease file
  nothing changes: every verb works as before. Once a lease is held, every
  command that changes the device (`ui tap|swipe|type|key|pinch|rotate|shake`,
  `open`, `permissions`, `location set|clear`, `media add`, `session
  launch|force-stop|clear|uninstall`, `network attach|detach|stop`,
  `devices shutdown`, mutating `simulator` and `ui accessibility` actions, and
  every mutating flow step) is admitted only for the holder's claim, checked
  just before the device is touched; a refusal exits 2 with the holder,
  `generation`, `capability: "control.lease"` and a hint, and nothing ran.
  Reading the screen, screenshots, recordings, logs, journal, `session list`,
  `session stop` and `control status` are never refused. `ui tree` still
  answers under someone else's lease but skips its accessibility recovery.
- **Claim** with `--control-holder ID` and `--control-generation N` (before or
  after the verb, exact spellings only) or with `AUTONOM_CONTROL_HOLDER` and
  `AUTONOM_CONTROL_GENERATION`; `control acquire` prints both as `env`.
  `--operation-id ID` names the action in the journal (`operation_id`). An
  admitted command's JSON gains `control: {generation, holder_id,
  operation_id}` only when a lease exists or a claim was given.
- An agent lease lasts `--ttl` seconds (default 60, 5..3600): renew it during
  long work (a renew without `--ttl` keeps the lease's own TTL), or the next
  action fails `control_lease_expired`. Never take
  over a live holder: `--takeover` is refused for an agent holder
  (`takeover_requires: "human"`) and `--kind human` is for people only. The
  Canvas's Take over replaces an agent at once, and the agent's next action
  fails `control_generation_stale`. The lease coordinates cooperating
  processes of one user; it is not a security boundary, so never reuse a
  holder id or generation you read from a refusal.
- **A person in the Canvas takes control by their first input** on a device
  nobody holds. From then on unclaimed agent actions on that device are
  refused `control_held` until the person presses Hand back, the Canvas's
  idle timeout releases it (120 s by default) or the page is closed (the
  lease expires within 30 s). Do not retry in a loop: poll `control status`
  and continue once it reads `free`.

## Related skills

- `mobile-screen` — UI tree, find, tap, screenshot
- `mobile-network` — HTTP(S) capture and mocking
- `ios-debugger-agent` / `android-debugger-agent` — build-and-run workflows
- `project-router` — when to load runtime skills at all
