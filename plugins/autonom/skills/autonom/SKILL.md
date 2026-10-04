---
name: autonom
description: Start here for Autonom — the universal mobile test/debug harness for AI agents on Android and the iOS Simulator. The map of the whole system — what it can do, how it works, and how to use it end to end (own a session, see the screen, drive it, watch logs and network, mock responses, capture evidence, journal every step). Read this first when asked to test, debug, profile, reproduce, or validate a running app, or when unsure which Autonom skill to load. Then let project-router narrow to the stack-specific skills.
---

# Autonom — how the whole harness works

Autonom lets an AI agent **test and debug a real mobile app** on an Android
emulator or an iOS Simulator, with evidence. You describe a goal in plain words;
this skill is the map that tells you what the harness can do and how to drive it.
The same verbs work on both platforms.

## When this applies

Any task about a *running* app: reproduce a bug, walk a flow, check a screen,
read logs, see what the app sent and how it reacts to a bad response, profile,
or validate a release. If the task is only about *writing* code, route straight
to the stack skills via `project-router`.

## How it is built (the mental model)

Three layers, and it helps to know which is which:

1. **The CLI control plane** — `scripts/autonom.py` (installed as `autonom`).
   One dependency-free JSON API. Every command prints JSON; an expected failure
   prints `{"ok": false, "error_code": "...", "hint": "..."}` on stderr with exit
   code 2, so you branch on a stable code, never parse prose. This is the source
   of truth; the skills are how to use it well.
2. **Thin verb-skills** — `mobile-session`, `mobile-screen`, `mobile-network`
   wrap the CLI verbs with the judgement to use them.
3. **Knowledge** — `mobile-memory` (`~/.autonom/apps/<pkg>/`) carries what was
   learned about an app once, so a flow is replayed, not re-derived.

Sessions, mocks, the process registry, and per-app knowledge are all
**machine-global** under `~/.autonom/` — a run is not tied to the directory it
was launched from, and the active session is found from anywhere.

## First moves

At the start of a new Autonom task, check the installed plugin and CLI versions
against their configured source once. Use `autonom-setup` and its
[update procedure](../autonom-setup/references/updates.md) to download and
install a newer version when needed. Keep the existing agent scope and source;
defer updates while a device session is active so its tools stay consistent.
If the source cannot be reached, report that the update check is inconclusive
and work with the installed version.

```bash
autonom doctor          # what can this machine actually do? tools, session, orphans
autonom devices         # Android + iOS in one list; each entry has a `running` flag
```

`doctor` is honest: green means installed, not proven. If something is missing it
names the exact fix.

## The end-to-end loop

```bash
# 1. Own a target (boot it first if needed)
autonom devices boot --avd Pixel_9                 # or --udid <sim>; session start also boots a sim
autonom session start --serial emulator-5554 --app-id com.example.app

# 2. See the screen, then act by meaning (not coordinates)
autonom ui tree                                    # compact accessibility tree
autonom ui tap --desc "Log In"
autonom ui type "test-user"                        # into the focused field

# 3. Watch what happens
autonom session outputs                            # what is followable + tail -f hints
autonom logs follow --source device --grep 'Error' --max-seconds 60   # live, bounded NDJSON
autonom network start --i-understand-mitm          # decrypt + record (consent-gated)
autonom network attach --i-understand-mitm
autonom network requests list --host api.example.com --status 401
autonom metrics snapshot --label baseline          # memory/CPU point, honest semantics

# 4. Force a failure and check the UI reacts
autonom network mock add --url '.../v1/login' --status 500 --json '{"error":"x"}'
autonom ui tap --desc "Log In"
autonom ui find --text "Something went wrong"

# 5. Capture evidence + record what you did
autonom screenshot --task login --label "500 error state"
autonom note add "login shows the generic error banner on 500; retry works" --task login

# 6. Read the run back, then clean up
autonom journal                                    # the full timeline of this session
autonom session stop                               # tears down proxy/stream best-effort
```

Everything from step 1–6 is appended to the session **journal** automatically
(`~/.autonom/sessions/<id>/journal.ndjson`): every verb, its scrubbed arguments,
the result, and your notes. `autonom journal --kind note` / `--verb 'ui tap'`
reads it back — the record you use to re-check or hand off a flow.

## What you can do (verb catalog)

| Area | Verbs |
| --- | --- |
| Discover | `doctor`, `devices`, `devices boot/shutdown` |
| Session | `session start/show/stop/launch/force-stop/clear/uninstall` |
| Screen | `ui tree/find/wait/tap/swipe/type/key` (`ui wait --settled`, `ui tree --format outline`); `find`/`tap` resolve on-screen matches first, and a node that is not on screen is `visible: false` in the tree and refused by `tap` (`element_offscreen`); `pinch/rotate/shake` are refused on both platforms; iOS input via idb or AXe, named in `backend` |
| First run | `tour` — overview, the workflow, this machine's targets, and `--run` to walk three screens into Settings: before/after screenshots, hierarchy, and device log attached to each tap or back, an assertion that the screen changed after each, an HTML/JUnit report, and a written account |
| Evidence | `screenshot` (with `width`/`height`), `shots list/show`, `record start/stop`, `note add/list`, `journal` |
| Deterministic state | `simulator status-bar pin\|clear` (both platforms), `simulator keyboard pin\|reset\|show` (iOS), `simulator animations pin\|reset\|show` (Android) — remove clock, battery, animation, and autocorrect noise from before/after comparisons; `verified` only after a read-back |
| Device state | `open` (deep link; Android names the activity that took it in `handled_by`), `permissions`, `location` (iOS + Android emulator), `media add`, `file ls/pull` |
| Diagnostics | `logs tail`, `crash list/show` |
| Live observation | `session outputs`, `logs follow`, `network requests follow`, `journal --follow` — bounded NDJSON streams |
| Metrics | `metrics snapshot/series/list-presets`, `metrics memory capture/analyze/warn`, `metrics frames …`, `metrics trace --preset …` |
| Network | `network start/stop/status/attach/detach`, `network requests list/show`, `network mock …`, `network export --har` |
| Flows | `flow check/fmt/list/run` — repeatable Flow v1 files with exact selectors and failure classes |
| Housekeeping | `processes`, `cleanup` |

## Which skill for what

- **`project-router`** — load next: classifies the repo and narrows to the
  stack-specific skills. This skill is the map; the router is the dispatcher.
- **`toolchain-doctor`** — read `autonom doctor`, inspect SDK/toolchain state.
- **`mobile-session`** — own a target, the artifact dirs, the journal.
- **`mobile-screen`** — trees, semantic find/tap, gestures, screenshots.
- **`mobile-network`** — capture, mock, HAR — consent-gated MITM.
- **`mobile-flow`** — repeatable Flow v1 files: author, validate, run, replay.
- **`mobile-memory`** — read/write per-app knowledge and flow runbooks.
- **`android-debugger-agent` / `ios-debugger-agent` / `flutter-debugger-agent`**
  — build-run-inspect for a specific stack.
- Domain packs — `flutter-*`, native `android-*` / `compose-*` — for testing,
  performance, memory, release validation, platform layers.
- **`autonom-setup`** — install, check for updates, refresh the plugin and CLI,
  or hand the harness off to another machine/agent.

## The evidence ladder

Each rung is stronger evidence than the one before it. Climb as far as the
claim needs, and say which rung the evidence reached:

```
code → unit/widget test → integration on an explicit target →
screenshot + UI tree + logs → profile / memory / network → before/after replay
```

## Rules that keep the work honest

- **Consent is real.** `network start/attach` decrypt traffic and change device
  config: they need their `--i-understand-mitm` (and `--install-ca`) flag every
  time, plus a typed phrase on a terminal. Consent is never cached.
- **Exit 0 ≠ it worked.** A tap returning ok does not prove the screen changed.
  Compare before/after trees or screenshots.
- **A mocked screenshot is a lie waiting to happen.** Captures taken while a mock
  is active are flagged `screenshot_shows_mocked_data`; never present them as real
  backend behaviour.
- **Never surface secrets** — tokens, keystores, `.env`. The journal and captures
  mask credential-shaped values by default; keep it that way.
- **One explicit target.** With more than one device attached, pass `--target` /
  `--serial` / `--udid`; ambiguity is an error listing candidates, never a guess.
- **iOS text lives in `desc`** (from `AXLabel`), not `text` — select with `--desc`.

## Where things live

```
~/.autonom/sessions/<id>/   shots, trees (full history), logs, network, recordings,
                            crashes, files, journal.ndjson, session.json,
                            flows/<run_id>/events.ndjson  (flow run evidence)
~/.autonom/apps/<package>/  per-app knowledge: app.md, flows/, bodies/  (mobile-memory)
~/.local/state/autonom/     mocks registry, process registry  (machine-level, persistent)
```
Flow *files* are repository source (conventionally `.autonom/flows/` in the
app repo — see `mobile-flow`); only run artifacts land under `~/.autonom`.

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

## Related

- `project-router` — the next skill to load; narrows to the stack.
- `mobile-session`, `mobile-screen`, `mobile-network`, `mobile-memory` — the verbs.
- `docs/USAGE.md`, `docs/CAPABILITIES.md`, `AGENTS.md` — deeper reference.
