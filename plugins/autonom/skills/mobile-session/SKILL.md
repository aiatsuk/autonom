---
name: mobile-session
description: Own an explicit Android device or iOS simulator session for AI agents — list targets, start Autonom session artifacts, install/launch/force-stop/clear apps, and stop cleanly with evidence directories.
---

# Mobile Session (Android + iOS Simulator)

## Purpose

Give the agent a **single explicit target** and an artifact directory before UI,
log, or network work. Prefer this skill over ad-hoc `adb` or `simctl` when testing
or debugging.

One verb set covers both platforms. `--serial` remains a permanent Android alias
of `--target`, so existing Android workflows are unchanged.

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

### Android

```bash
python3 <autonom-root>/scripts/autonom.py session start --serial emulator-5554 --app-id com.example.app
python3 <autonom-root>/scripts/autonom.py session launch com.example.app            # resume where it was
python3 <autonom-root>/scripts/autonom.py session launch com.example.app --fresh    # launcher activity on a cleared task
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
```

The journal is secret-safe (typed text and body/header values are masked) and
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
   (device log stream, process output, network flows, `metrics/` artifacts)
   with a `tail -f` hint for a human's second terminal.
4. `session stop` tears down the log stream, recorder, and proxy best-effort and
   reports each action — it never fails because teardown failed.
5. If a session died without stopping, `autonom doctor` lists orphaned processes,
   any device left pointing at a dead proxy, and any emulator still routed
   through another session's *live* proxy (`device_attached_to_foreign_proxy`).

## Failure codes worth knowing

| Code | Meaning |
| --- | --- |
| `ambiguous_target` | more than one ready target; pass `--target` |
| `idb_required` | iOS `ui` verbs need idb; `screenshot`/`logs`/`open` still work |
| `ios_boot_failed` | simulator never reached `Booted`; try `xcrun simctl erase <udid>` |
| `no_active_session` | start one with `session start` |
| `usage_error` | argparse rejected the argv; `hint` carries the usage line |
| `app_not_debuggable` | `file ls`/`pull` need a debuggable build; release and system apps refuse run-as |
| `simulator_must_be_shutdown` | `simulator keyboard pin` needs a shut-down simulator; pass `--value reboot=true` or shut it down first |
| `simulator_data_not_found` | the simulator has no data directory yet — boot it once, or set `AUTONOM_CORESIMULATOR_DEVICES` |

## Related skills

- `mobile-screen` — UI tree, find, tap, screenshot
- `mobile-network` — HTTP(S) capture and mocking
- `ios-debugger-agent` / `android-debugger-agent` — build-and-run workflows
- `project-router` — when to load runtime skills at all
