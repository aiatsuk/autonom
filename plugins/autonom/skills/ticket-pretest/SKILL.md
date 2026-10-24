---
name: ticket-pretest
description: Pre-test one ticket and its diff on an Android emulator or iOS Simulator before it goes to QA, with the `autonom pretest` verbs. Read the ticket and diff context, plan checks before touching the device (acceptance points, then edge cases from the 18-case kit, then regression flows), drive each check under an action budget and forbidden-tap rules, record findings with expected vs actual and evidence refs, pack an integrity-checked report and state the verdict plainly. Also covers the QA review loop, draft regression flows, reproducibility, rerun after a fix, app profile and warm-up, and accessibility and analytics checks. Use when asked to pre-test, smoke-test or validate a ticket, PR, branch or diff on a device.
model: opus
effort: high
---

# Ticket pre-test

## Purpose

Check one ticket and the code change behind it on a real emulator or Simulator,
the way a careful QA engineer would, and leave a pack a person can trust:
which checks ran, what each showed, what failed, what was never covered, and the
evidence for every claim.

`autonom` below means `python3 <autonom-root>/scripts/autonom.py`. Every verb
prints JSON; an expected refusal exits 2 with `error_code` and `hint` on stderr.
Branch on the code, never on prose.

The pre-test lives inside the current Autonom session
(`~/.autonom/sessions/<id>/pretests/<pretest-id>/`). It adds no new way to drive
the device: you still use `ui`, `screenshot`, `network`, `simulator`, `open`,
`session launch` and `flow run`. The pre-test files the evidence under the open
check, counts mutating actions against the budget and refuses forbidden taps.

Load `mobile-session` and `mobile-screen` with this skill; add `mobile-network`
for network cases and analytics, and `mobile-flow` for flows and drafts.

## The playbook

### 1. Own the device and start

```bash
autonom session start --serial emulator-5554 --app-id com.example.app --launch  # or --udid <sim>
autonom session launch com.example.app            # if the app is not running yet
cd <app repo>                                     # or pass --repo
autonom pretest start --ticket ticket.md --base main --ticket-ref APP-123 \
    --max-actions 200 --max-minutes 45            # --ticket - reads stdin
```

- Launch the app before `pretest start`: the start takes the health baseline,
  and its memory and CPU snapshot needs a running app (otherwise the metrics
  block is `unavailable` for the whole pre-test).
- `session launch` and `session force-stop` always take the app id:
  `autonom session launch com.example.app`, `autonom session force-stop com.example.app`.

- `--base` is the ref the change is compared with; `--head` defaults to the
  working tree (committed diff plus untracked files).
- Defaults: 300 actions, 60 minutes, forbidden text
  `buy|pay|purchase|delete|remove account|купить|оплатить|удалить`, only the
  session's app allowed. `--forbid-text PATTERN` (case-insensitive regex,
  repeatable) replaces the default list, so repeat the defaults you still want;
  `--allow-app ID` (repeatable) names the apps `open` and launches may reach,
  so include the app under test.
  Set them only from the ticket's needs; never widen them to get past a
  refusal.
- Every guarded point tap reads a fresh UI tree before it is sent, so rapid
  repeat taps are slow during a pre-test (about 2.5 s each). A race criterion
  (the `double-tap` kit case: double tap, double submit) passes only with an
  observed request count: count the requests in `network requests list` and
  end with `pretest check end <id> --verdict pass --observed-count N` (an
  integer, 0 or more; it is stored on the check and shown in `summary.md` and
  `summary.json`). A pass without the count is refused (`race_needs_count`);
  when you cannot count, end the check `inconclusive` and say why in `--note`.
- The ticket is masked for credentials before it is written. Masking is best
  effort over known shapes, so still do not paste secrets into a ticket file.
- One pre-test per session: `pretest_already_active` means finish or
  `pretest stop` the other one first.

### 2. Read before you touch

```bash
autonom pretest context        # changed files, hunk ranges, selected flows, screen hints, profile
autonom pretest kit            # the 18 edge cases (and coverage once checks exist)
```

Read the ticket and `context` together. Note:

- `changed_files` with `ranges` and `functions` — what actually changed.
  `diff_truncated: true` means hunks were cut; every file is still named.
- `flows.selected` — existing flows whose `covers` match the diff;
  `flows.uncovered_files` — changed files no flow covers (they need hand checks).
- `runtime_map_hints` — screens that may show the change. They are hints
  (`hint: true`), never coverage.
- `profile` — the app profile in use, or `null`.

### 3. Plan every check before driving the device

Add all planned checks first, in this order, each with a short `--why`:

1. **Acceptance points** — one `--kind ticket` check per testable statement in
   the ticket. When an acceptance point is itself a kit case (the ticket asks
   for an error state on a 500, an offline banner, a deep link), also set
   `--case <kit-id>` on that ticket check, so the kit coverage counts it and
   you do not plan the same case twice.
2. **Edge cases** — pick the kit cases the diff can plausibly break (a network
   call changed: `offline`, `server-error`, `timeout`; a form: `empty-input`,
   `long-input`, `emoji-input`; a screen with state: `background-resume`,
   `process-death`, `rotation`). One `--kind edge --case <kit-id>` check each.
   Skip cases that do not apply; the kit table reports them as not covered,
   which is honest.
3. **Regression** — the selected flows run as checks of their own through
   `pretest flows` (step 5). Add `--kind network`, `a11y` or `analytics` checks
   when the ticket touches requests, a new screen, or tracking.

```bash
autonom pretest check add "Results appear after a search" --kind ticket \
    --why "acceptance point 1"
autonom pretest check add "Search offline" --kind edge --case offline \
    --why "the diff changes the search request"
autonom pretest check add "A 500 shows an error and Retry" --kind ticket \
    --case server-error --why "acceptance point 2 (also the kit case)"
autonom pretest check list
```

A plan written before driving keeps the pre-test honest: a check that was
planned and never run shows up as `not_covered` instead of silently vanishing.

### 4. Run each check: begin, act, look, end

```bash
autonom pretest check begin chk-1               # one open check at a time
autonom ui tree                                 # see the screen first
autonom ui tap --text "Search"                  # counted against the budget
autonom ui type "blue mug"
autonom ui wait --settled                       # let the results load
autonom ui find --text "Blue mug"               # state the expected result as a find
autonom screenshot --label "results"            # filed under chk-1 automatically
autonom pretest check end chk-1 --verdict pass --note "3 results, first is Blue mug"
```

- Everything done while a check is open (journal entries, screenshots, saved
  trees, action details) carries the check id. Network requests and log lines
  are attributed by the check's time window when the pack is built.
- Drive by meaning: selector taps (`--text`, `--desc`, `--resource-id`) and a closing
  `ui find` on the expected state. That is what `flow-draft` can compile later;
  coordinate taps cannot be compiled.
- `ui find` reads the screen once; it does not wait or retry. After an action
  that loads, run `ui wait --settled` first, or put the wait in a flow
  (`assertVisible` polls until its timeout).
- `ui swipe --duration` is in seconds (`--duration 0.4`), not milliseconds;
  values over 60 are refused (`invalid_value`).
- Exit 0 is not proof. Compare the tree or a screenshot before and after.
- Verdicts: `pass` (the expected state was seen), `fail` (it was not, or
  something broke), `blocked` (the check could not be carried out: refusal,
  dead device, missing data), `not_covered` (decided not to run it, say why),
  `inconclusive` (ran, but the evidence does not decide it). Write the reason
  in `--note`.
- Set up and undo each edge case with its verbs from the kit table, and restore
  the device before the next check (network online, mocks removed, appearance
  and text size back, app relaunched).
- Clipboard (a "Copy" button, a shared link): on Android
  `simulator clipboard get` is refused with `clipboard_unavailable`, because
  Android 10 and later do not let a shell read the clipboard. Paste instead:
  tap a text field, long-press it, tap Paste (`ui tap --text 'Paste'`), then
  read the pasted text with `ui find --text '<expected>'`. Use a field whose
  content you may change, and clear it afterwards. On iOS
  `simulator clipboard get` reads the Simulator clipboard directly.
- App language on iOS: set it for one launch with launch arguments, e.g.
  `autonom session launch com.example.app --fresh --arg=-AppleLanguages --arg='(ru)' --arg=-AppleLocale --arg=ru_RU`
  (the `=` form keeps a value that starts with `-` from being read as a flag).
  The next launch without them is back to the device language.

### 5. Run the regression flows

```bash
autonom pretest flows [--env KEY=VALUE]... [--secret NAME]...
```

Each flow selected in `context` becomes a `regression` check, begun and ended
automatically with its `run_id`: passed→`pass`, failed→`fail`, broken→`blocked`,
all steps skipped→`inconclusive`. Flow steps count against the budget. The
output lists each flow with its `check_id`, `run_id`, `status` and `verdict`
(`flows`).

A flow that needs live data the environment does not have (an account with
orders, a seeded catalogue) fails or breaks for that reason, not because of the
change. That is an environment block. An ended check cannot be ended again
(`no_open_check`); correct its verdict instead:

```bash
autonom pretest check amend chk-3 --verdict blocked \
    --note "needs an account with orders; the test account has none"
```

`check amend <id> --verdict V --note TEXT` works on ended checks of the active
pre-test only (`check_not_ended` for a check that is not ended yet,
`pretest_finalized` after `pack`). It adds an `amended` entry (old verdict, new
verdict, note, time) to the check's history, and the summary and pack use the
latest verdict. Also say in your report which data was missing. When you run
such a flow by hand inside your own check, end that check `blocked` with the
reason in `--note`.

### 6. Record findings: expected vs actual, with evidence

```bash
autonom pretest finding add --check chk-2 --severity major \
    --title "Server error shows an empty list" \
    --expected "an error message with a retry button" \
    --actual "an empty results list, no message" \
    --evidence shot:4 --evidence request:7f3a
autonom pretest finding list
```

- One finding per distinct problem. `--actual` is what you saw, in plain words;
  `--expected` is what the ticket or common sense requires.
- Severity: `blocker` (the ticket's main path is unusable, crash, data loss),
  `major` (an acceptance point fails or a likely edge case breaks badly),
  `minor` (cosmetic or rare, with a workaround), `info` (worth knowing, not a
  defect).
- Evidence refs are checked when written (`evidence_ref_unknown` writes
  nothing): `shot:<n or file>` (row of `autonom shots list`), `tree:<file under
  trees/>`, `action:<file stem under actions/>`, `request:<id>` (from
  `autonom network requests list`), `log:<from-ISO>..<to-ISO>`, `run:<run_id>`,
  `crash:<name>` (from `autonom crash list` or a health report).
- Separate facts from guesses. The finding states what was observed; a guess
  about the cause goes in your report marked as a guess, never in `--actual`.
- A screenshot taken while a mock is active is flagged as mocked data (a
  "Mocked" tag in `index.html`, "(mocked data)" in `summary.md`). Say so; never
  present it as real backend behaviour.
- The steps to reproduce a finding run from its check's start to its latest
  evidence item, so cite the screenshot or request that shows the problem.

### 7. Watch device health

```bash
autonom pretest status         # actions used and left, minutes, open check, counts
autonom pretest health         # new crashes, grouped error logs, failed requests, metric leads
```

On iOS, start the session with `--log-stream`
(`autonom session start --udid <sim> --app-id <app-id> --log-stream`) so that
`pretest health` and `pack` see the app's logs; without it the log part of
health is `unavailable`. Android reads logcat without it.

`health` (also run by `pack`) compares with the baseline taken at start and
writes automatic findings (`origin: auto`, `severity: info`,
`confirmed: false`). They never change a verdict by themselves. Open each one;
if it is a real problem tied to the ticket, record your own finding with the
evidence. A capability that could not be read is `unavailable`, never zero.

### 8. Pack and report

```bash
autonom pretest pack                       # or --out DIR (empty or new)
autonom report verify <pack>
```

`pack` ends an open check as `inconclusive` ("open at pack time"), computes
health and the ticket verdict, and writes `summary.md`, `summary.json`, an
offline `index.html`, the evidence and nested flow bundles, sealed by
`integrity.json`. Re-running `pack` returns the same pack unchanged. A packed
pre-test refuses changes (`pretest_finalized`).

The ticket verdict is fixed by rule, in this order:

1. `fail` — any check failed, or any confirmed `blocker`/`major` finding.
2. `blocked` — any check blocked.
3. `not_covered` — any planned check never began, or there are no checks.
4. `inconclusive` — any check inconclusive or left open.
5. `pass` — otherwise.

Report it plainly: the verdict, the failed and blocked checks, what was not
covered and why, the findings with severity, open automatic findings, and the
pack path. Never round a `not_covered` or `inconclusive` up to `pass`, and
never call the ticket done because flows passed if acceptance checks were not
run. Paste from `summary.md`; it holds pack-relative paths only.

## After the pack

### QA review

```bash
autonom pretest review <pack> approve [--confirm f-3] [--dismiss f-4] --author <name>
autonom pretest review <pack> return-to-dev --finding f-1 --note "500 path missing"
autonom pretest review <pack> more-checks --check "Rotate on results"
autonom pretest review-status <pack>
```

The decision is written under the pack's `annotations/`, outside the sealed
set, so `report verify` stays ok. `return-to-dev` needs a `--finding`;
`--confirm`/`--dismiss` take automatic findings only (`not_automatic`).
`more-checks` starts a linked child pre-test in the active session with the
parent's ticket and context and the requested checks already added. It uses
this session's app and default safety rules (or the review's `--app-id`,
`--max-actions`, `--max-minutes`, `--forbid-text`, `--allow-app`), never the
pack's. Run those checks with the same playbook, then pack the child.

### Draft a regression flow, measure reproducibility

```bash
autonom pretest flow-draft f-1             # <repo>/.autonom/flows/drafts/<ticket-slug>-f-1.yaml
autonom flow check <draft path>
autonom pretest repro f-1 --times 3        # reproduced k/3, stable | intermittent | not_reproduced
```

The draft is compiled from the journal of the finding's check, so it is only as
good as the check: selector taps and a closing `ui find`. Read its last
assertion before trusting it; a failing repro run reproduces the finding, a
passing one does not. An intermittent finding stays a finding. `repro` records
nothing when a run ends without a verdict (broken, refused, dead device) and
works on the active pre-test only; for a pack use
`pretest flow-draft f-1 --pack <pack>` (reads, records nothing).

### Rerun after a fix

```bash
# the fix lands; rebuild and install the app; no other pre-test active
autonom pretest rerun <pack> --check chk-2     # default: every check with a flow or draft
autonom pretest pack                           # the child shows chk-2 before and after
```

The child pre-test (`kind: rerun`) runs each chosen check's flow or draft on the
current build. Its summary shows `before` (parent verdict and evidence) and
`after` per check; the parent pack is only read.

## App profile, warm-up, accessibility, analytics

- **Profile** — `<repo>/.autonom/apps/<app-id>/profile.yaml`, else
  `~/.autonom/apps/<app-id>/profile.yaml`. Keys: `login_flow`, `warmup_flow`,
  `accounts` (environment variable names only, and only names starting with
  `AUTONOM_` or `PRETEST_`; any other name is `profile_invalid`), `fixtures`, `flaky_screens`,
  `events`. A value shaped like a credential refuses the profile
  (`profile_invalid`) and `pretest start` creates nothing. Put the secrets in
  the environment, never in the profile.
- **Warm-up** — `autonom pretest warmup` (no check open) runs `login_flow` and
  `warmup_flow`, updates the Runtime Map and reports each hinted screen
  `reached`, `unreplayable`, `known` or `unknown`. It counts against the budget.
  Use it once after `start` when the app needs a login or seeded state.
- **Accessibility** — with a `--kind a11y` check open on the screen in question:
  `autonom pretest a11y`. It writes automatic `minor` findings for unlabelled
  controls, targets under 48 dp (Android) or 44 pt (iOS), and duplicate labels,
  with bounds and the saved tree. A control is any node that is clickable,
  long-clickable or checkable, or that looks interactive without saying so: a
  button, link, switch, checkbox, radio, slider or text field by its class or
  role (Android class names, iOS `XCUIElementType…` and `AX…` names), or a node
  with an action trait. It does not check contrast or reading order.
- **Analytics** — needs captured traffic (`network start` and `network attach`
  with `--i-understand-mitm`) and `events` rules in the profile. With a
  `--kind analytics` check open, do the action, then `autonom pretest events`:
  `matched` (with request ids), `missing` (each an automatic finding) and
  `unexpected`. Body values are never copied. Without capture the result is
  `state: unavailable`; end that check `blocked`, not `pass`.

Automatic findings from `a11y`, `events` and `health` are unconfirmed. Confirm
the real ones with your own finding or leave them for QA's `--confirm`.

## The edge-case kit

`--case` takes one of these ids. Verbs are listed in the order you run them;
undo the setup before the next check. Full setup, restore and platform notes:
[references/edge-case-kit.md](references/edge-case-kit.md). The list mirrors
`scripts/autonom_lib/pretest/kit.py`; `autonom pretest kit` prints the live one.

| Id | Case | Verbs |
| --- | --- | --- |
| `offline` | No network | `simulator network offline`, `simulator network online` |
| `slow` | Slow network | `network start`, `network attach`, `network mock add --match '<url glob>' --delay 3`, `network requests list`, `network mock remove <id>` |
| `server-error` | Server error (5xx) | `network start`, `network attach`, `network mock add --match '<url glob>' --status 500`, `network mock remove <id>` |
| `timeout` | Request never answers | `network start`, `network attach`, `network mock add --match '<url glob>' --hang`, `network requests list`, `network mock remove <id>` |
| `background-resume` | Background and resume | `ui key HOME`, `session launch <app-id>` |
| `process-death` | Process death and restore | `session force-stop <app-id>`, `session launch <app-id>` |
| `rotation` | Rotation | `flow run (setOrientation: landscape)`, `flow run (setOrientation: portrait)` |
| `dark-mode` | Dark mode | `simulator appearance dark`, `simulator appearance light` |
| `large-text` | Large text | `simulator text-size 1.3 (Android scale)`, `simulator text-size accessibility-extra-large (iOS)`, `simulator text-size 1.0 (Android) or large (iOS)` |
| `locale` | Another locale | `simulator keyboard pin --value locale=<locale>`, `simulator keyboard reset` |
| `permission-denied` | Permission denied | `permissions revoke <service> <app-id>`, `permissions reset <service> <app-id>` |
| `empty-input` | Empty input | `ui tap --text '<field>'`, `ui tap --text '<submit>'` |
| `long-input` | Very long input | `ui tap --text '<field>'`, `ui type '<500 characters>'` |
| `emoji-input` | Emoji input | `ui tap --text '<field>'`, `ui type '<text with emoji>'` |
| `rtl-input` | Right-to-left input | `ui tap --text '<field>'`, `ui type '<right-to-left text>'` |
| `double-tap` | Double tap | `flow run (doubleTapOn: <button>)`, `ui tap --text '<button>' (twice, quickly)` |
| `fast-back` | Back while loading | `ui tap --text '<action>'`, `ui key BACK` |
| `deep-link` | Deep link | `open '<app link>'`, `session force-stop <app-id>`, `open '<app link>'` |

Platform limits: `simulator network` (offline, and shaping every request) is
Android-emulator only and refused on iOS (`unsupported_capability`); `simulator keyboard`
(locale) is iOS only; `setOrientation` and `ui key BACK` are Android only. When
a case cannot be set up on the target, end its check `not_covered` with the
reason rather than faking it.

Slow and timeout on one request (both platforms, with capture on): the table
slows or stalls just the request the ticket is about with a mock. Answer a
`--delay` mock with a body (`--json '<body>'`) when the app needs one:

```bash
autonom network mock add --match '<url glob>' --json '<body>' --delay 3   # answers after 3 s
autonom network mock add --match '<url glob>' --hang   # held until the mock is removed or disabled, or 120 s
autonom network requests list                           # an unfinished request: completed false, error_kind
autonom network mock remove <id>
```

`--delay N` takes seconds, more than 0 and at most 120 (`2.5` is fine); other
requests are not held. `--hang` cannot be combined with `--delay`; the held
request goes on upstream when you `network mock remove` or
`network mock disable` the rule, and after 120 s it is killed.
`network mock update <id> --delay N` (or `--hang`) changes an existing rule. A
request that never completed (held to the cap, cancelled by the app, an
upstream error) is recorded with `completed: false` and an `error_kind`
(`hang_cap`, `client_abort`, `upstream_error`, `other`); it shows in
`network requests list` without a status and in the pack's requests per check
with `completed: false`. That record is the proof the request really never
answered: cite it as `request:<id>` next to the screenshot of the stuck or
timeout screen. Remove the mock before the next check.

## Guard rails and refusals

| Code | Meaning | What to do |
| --- | --- | --- |
| `budget_exhausted` | the action or minute budget is spent | stop driving; end the open check `blocked`, pack, report it |
| `forbidden_action` | the tap target matches forbidden text, or the tree at a point could not be read | do not retry another way; end the check `blocked` with the reason, or pick a safe path |
| `app_not_allowed` | `open` or a launch would reach an app outside the allow list | treat as blocked; only the user may widen `--allow-app` |
| `check_already_open` | another check is open | end it first |
| `no_open_check` / `unknown_check` | the id is not the open check / not in this pre-test | `pretest check list` |
| `evidence_ref_unknown` | a ref does not resolve | list the artifacts and use the real id |
| `pretest_finalized` | the pre-test is packed | use `review more-checks` or `rerun` |
| `race_needs_count` | `check end --verdict pass` on a `double-tap` check without `--observed-count` | count the requests (`network requests list`) and pass `--observed-count N`, or end `inconclusive` |
| `clipboard_unavailable` | Android does not let a shell read the clipboard | paste into a text field and read it with `ui find` (step 4) |
| `no_active_pretest` / `pretest_already_active` | none active / one already active | `pretest list`, then `start` or `stop` |
| `pretest_damaged` | the active pre-test's `pretest.json` cannot be read | `pretest status` shows `status: damaged` and the reason; `pretest stop --force` (or restore the file) |
| `check_not_ended` | `check amend` on a check that is not ended yet | end it with `check end` first |

Refusals happen before any input reaches the device and are journaled. Never
work around one: no coordinate tap next to forbidden text, no direct `adb` or
`simctl` input. Read-only verbs (`ui tree`, `ui find`, `screenshot`, `pretest`
verbs) are never counted.

`autonom pretest stop` closes a pre-test without a pack; `pretest pack` can
still pack a stopped one later. `autonom pretest list` shows the session's
pre-tests.

A damaged pre-test (its `pretest.json` unreadable or invalid) answers
`pretest status` with `status: damaged` and the reason, and every guarded
device action refuses with `pretest_damaged` before any input. Stop it with
`autonom pretest stop --force`: the damaged file is kept as
`pretest.json.damaged` and the pre-test is marked stopped. Say in your report
that the pre-test was damaged and what you did about it.

## Related

- `autonom` — the map; `project-router` — routing.
- `mobile-session`, `mobile-screen`, `mobile-network`, `mobile-flow`,
  `mobile-memory` — the verbs a check is made of.
- `docs/USAGE.md` (Pre-test a ticket) and `docs/CAPABILITIES.md` (Ticket
  pre-test) in the Autonom repository.
