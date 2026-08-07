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
autonom logs tail --package com.example.app --since 60
autonom network start --i-understand-mitm          # decrypt + record (consent-gated)
autonom network attach --i-understand-mitm
autonom network requests list --host api.example.com --status 401

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
| Screen | `ui tree/find/tap/swipe/pinch/rotate/shake/type/key` |
| Evidence | `screenshot`, `shots list/show`, `record start/stop`, `note add/list`, `journal` |
| Device state | `open` (deep link), `permissions`, `location` (iOS + Android emulator), `media add`, `file ls/pull` |
| Diagnostics | `logs tail`, `crash list/show` |
| Network | `network start/stop/status/attach/detach`, `network requests list/show`, `network mock …`, `network export --har` |
| Housekeeping | `processes`, `cleanup` |

## Which skill for what

- **`project-router`** — load next: classifies the repo and narrows to the
  stack-specific skills. This skill is the map; the router is the dispatcher.
- **`toolchain-doctor`** — read `autonom doctor`, inspect SDK/toolchain state.
- **`mobile-session`** — own a target, the artifact dirs, the journal.
- **`mobile-screen`** — trees, semantic find/tap, gestures, screenshots.
- **`mobile-network`** — capture, mock, HAR — consent-gated MITM.
- **`mobile-memory`** — read/write per-app knowledge and flow runbooks.
- **`android-debugger-agent` / `ios-debugger-agent` / `flutter-debugger-agent`**
  — build-run-inspect for a specific stack.
- Domain packs — `flutter-*`, native `android-*` / `compose-*` — for testing,
  performance, memory, release validation, platform layers.
- **`autonom-setup`** — install or hand the harness off to another machine/agent.

## The evidence ladder

Climb it; do not skip:

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
                            crashes, files, journal.ndjson, session.json
~/.autonom/apps/<package>/  per-app knowledge: app.md, flows/, bodies/  (mobile-memory)
~/.local/state/autonom/     mocks registry, process registry  (machine-level, persistent)
```

## Related

- `project-router` — the next skill to load; narrows to the stack.
- `mobile-session`, `mobile-screen`, `mobile-network`, `mobile-memory` — the verbs.
- `docs/USAGE.md`, `docs/CAPABILITIES.md`, `AGENTS.md` — deeper reference.
