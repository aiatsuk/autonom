---
name: mobile-session
description: Own an explicit Android device or iOS simulator session for AI agents — list targets, start Autonom session artifacts, install/launch/force-stop/clear apps, and stop cleanly with evidence directories.
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
| `idb_required` | iOS `ui` verbs need idb; `screenshot`/`logs`/`open` still work |
| `ios_boot_failed` | simulator never reached `Booted`; try `xcrun simctl erase <udid>` |
| `no_active_session` | start one with `session start` |
| `usage_error` | argparse rejected the argv; `hint` carries the usage line |
| `app_not_debuggable` | `file ls`/`pull` need a debuggable build; release and system apps refuse run-as |
| `simulator_must_be_shutdown` | `simulator keyboard pin` needs a shut-down simulator; pass `--value reboot=true` or shut it down first |
| `simulator_data_not_found` | the simulator has no data directory yet — boot it once, or set `AUTONOM_CORESIMULATOR_DEVICES` |

## Backend recovery and explicit sessions

Keep device actions inside the Autonom CLI, including screenshots and recovery.
`--session-id <id>` binds one command to that session (its artifacts and journal)
without changing the current session. A stopped session stays readable (`journal`,
`shots`, `logs`, `report` but not `report serve`, `session show|outputs`) and refuses
everything else with `session_stopped`; another target's flags are refused with `session_target_mismatch`.
One session per machine store still applies: `session start` refuses a second one.
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

## Related skills

- `mobile-screen` — UI tree, find, tap, screenshot
- `mobile-network` — HTTP(S) capture and mocking
- `ios-debugger-agent` / `android-debugger-agent` — build-and-run workflows
- `project-router` — when to load runtime skills at all
