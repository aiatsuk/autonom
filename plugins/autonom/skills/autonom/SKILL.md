---
name: autonom
description: Start here for Autonom — the mobile test/debug harness for AI agents on Android and the iOS Simulator. The short work loop for a running app — own a session, act and wait in one call, read what changed, capture evidence — and the map of recipes and deeper skills. Read this first when asked to test, debug, reproduce, mock, profile or validate a running app, or when unsure which Autonom skill to load.
model: inherit
---

# Autonom — the work loop

Autonom drives a real app on an Android emulator or an iOS Simulator and keeps
evidence. One CLI, `autonom`; the same verbs on both platforms. This page is
enough for a usual check. Open a recipe only for the step you are on.

## The loop

```bash
autonom devices                                    # pick one target
autonom session start --serial emulator-5554 --app-id com.example.app --launch --profile agent
autonom ui tree                                    # the screen, one line per node

# act, wait and keep evidence in ONE call; the reply is a few lines of text
autonom ui tap --text "Orders" --until gone:text=Loading --until text="Order #" --shot "orders list"
autonom ui type "test-user" --until text="Continue"
autonom ui key BACK

autonom session stop
```

When your host lists Autonom MCP tools (`tap`, `wait`, `screen`, `steps`, ...),
use them instead of the shell: the same verbs and replies, with no process
start per call; `until` and `shot` are tool arguments.

An action replies with what it did, `screen:` (the app and screen in front) and
`changes:` (`+` new, `-` gone, `~` changed nodes). Decide the next step from
that reply. Read the whole screen only when you need it: `ui tree`, or
`--screen` on the action. `--json` prints every key as one JSON line.

## Rules

1. **Never `sleep`.** Wait inside the call. On an action: `--until text=V`,
   `desc=V`, `id=V`, `gone:text=V`, `'request=*/v1/orders*'` (repeat it; all
   must hold). On their own: `autonom ui wait --text V [--gone]`, `ui wait
   --settled`, `autonom network requests wait --path GLOB`. Timeouts are
   milliseconds: `--until-timeout-ms`, `--timeout-ms`.
2. **Read the reply; do not discard it.** No `>/dev/null`, no `| tail`, no
   script around `autonom`: the text reply is already short.
3. **A line that starts with `!` needs you.** `! not on the fast path:` names
   why a slow backend served the call and how to get the fast one back: fix
   it before going on (`recipes/fast-path.md`). `! until_not_met` and
   `! wait_not_met`: the condition did not hold. After an action the input
   was already sent: look at the screen, never send it again. The same after
   `ui_action_uncertain`.
4. **Select by meaning:** `--text`, `--desc`, `--resource-id`; coordinates only
   for what the tree does not list. On iOS the visible label is in `desc`.
5. **A screenshot is evidence, not your eyes.** Take it with `--shot LABEL` on
   the action that reaches the state. Open an image only to judge its look.
6. **Steps you already know go in one call:** Flow steps on stdin to
   `autonom ui do`.
7. **One explicit target.** With several devices pass `--serial` or `--udid`.
8. **Consent and honesty.** Network capture needs `--i-understand-mitm` every
   time. A screenshot taken with a mock active shows mocked data: say so.
   Never print tokens or `.env` values.

## Android fast path

Reads and inputs go through Autonom's on-device helper: tens of milliseconds
a read, any Unicode with `ui type` and `ui fill`, `signals` that name a
keyboard or a dialog, and `screenshot --agent` as a small JPEG. Without it
the same commands use `uiautomator dump` and adb, about 2 s a read, and the
reply says so first (`fallback_reason`). The helper holds the device's
UiAutomation: before Appium or uiautomator2 uses the device run `autonom ui
helper stop`. `--ui-backend uiautomator` opts out on purpose.

## Recipes (open one when you need it)

| Need | Read |
| --- | --- |
| Pick a device, start, launch, stop, several sessions | [`recipes/session.md`](recipes/session.md) |
| Taps, typing, waits, `ui do`, reading the reply | [`recipes/act-and-wait.md`](recipes/act-and-wait.md) |
| See requests, mock or edit a response, wait for a request | [`recipes/mocks.md`](recipes/mocks.md) |
| Screenshots, notes, the journal | [`recipes/evidence.md`](recipes/evidence.md) |
| `! not on the fast path`, the helper, Appium beside Autonom | [`recipes/fast-path.md`](recipes/fast-path.md) |

## Deeper

- [`references/system-map.md`](references/system-map.md): how the harness is
  built, every verb, where files live, backend recovery, the update check.
- Other skills, only when a recipe is not enough: `mobile-screen`,
  `mobile-session`, `mobile-network`, `mobile-flow` (repeatable flows),
  `mobile-memory` (what is known about an app), `ticket-pretest`,
  `project-router` (the stack skills: Flutter, native Android, iOS),
  `flutter-debugger-agent` (hot reload and hot restart while you change
  Flutter code),
  `toolchain-doctor`, `autonom-setup` (install and update; use it when
  `autonom` is missing).
