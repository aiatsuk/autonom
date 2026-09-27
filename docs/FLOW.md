# Flow v1 — the Autonom flow language

A flow file is a strict YAML-subset document: header fields, one `---` line,
then a sequence of commands. It is designed so a human can read it without
training, an agent can generate it safely, and every mistake is refused with
`file:line:column` and a machine-stable `error_code` — never silently
reinterpreted.

```yaml
schema: autonom.dev/flow/v1
appId: com.example.app
name: Login
tags: [smoke, auth]
---
- launchApp
- tapOn: Sign in
- inputText:
    value: ${TEST_EMAIL}
    sensitive: true
- assertVisible:
    selector:
      id: home_screen
    timeoutMs: 7000
```

Check, format, and list flows without a device:

```text
autonom flow check .autonom/flows      # validates the whole runFlow graph
autonom flow fmt login.yaml --write    # canonical form (expands shorthand)
autonom flow list                      # file, id, name, tags, platforms
autonom flow run login.yaml            # execute against the active session
autonom flow run login.yaml --until-step 7 --evidence always
```

`platforms` in `flow check` and `flow list` is what the flow can really run
on: `requires.platform` when declared, otherwise both — minus iOS when a step
that runs there is Android-only (`back`, `clearState`, `setOrientation`,
`launchApp.clearState`, a `pressKey` with a `KEYCODE_*` key). A step inside a
`runFlow` guarded by `when: {platform: android}` does not count, and
`flow check` counts `runFlow` children too. A flow that declares `ios` in
`requires.platform` and still uses such a step is refused with
`unsupported_on_platform` (`unsupported_key_for_platform` for a key) at the
step's `file:line:column`; on an iOS target `flow run` refuses the same steps
at pre-flight, before any mutation, with the step's file and line.

`flow run` needs an active session (`autonom session start`), pre-flights the
whole flow against the resolved target before any mutation (`requires.platform`
and `requires.capabilities` included), and exits `0` on
pass, `1` on a *test failure* (summary on stdout with
`failure_class: test_failure`), `2` on definition/infrastructure errors
(stderr envelope). Events stream to
`~/.autonom/sessions/<id>/flows/<run_id>/events.ndjson` (or stdout with
`--events`); secrets pass via `--secret NAME` and never enter artifacts
(including `when.envEquals` skip reasons). An `--env NAME=VALUE` whose value
reaches a `sensitive: true` slot (directly or forwarded through a runFlow
`env:`) is treated as the secret it is: redacted from events, manifest and
reports, recorded under `secret_names`, reproduced as `--secret NAME`, and
named in a `flow_env_value_sensitive` run warning.
Directory runs execute a tag-filtered suite
(`flow run .autonom/flows --include-tag smoke --exclude-tag flaky`). Suite
discovery takes every `*.yaml` below the directory except App Skill overlays
(`.autonom/apps/**` — `selectors.yaml`, `fixtures.yaml`,
`compatibility.yaml` and promoted copies) and files whose header declares
another schema (`schema: autonom.selectors/v1` and friends); a file with a
Flow schema of any version, or a Maestro document, is always a member;
`runFlow` children execute inline with the root `appId` inherited and their
own env frame; a false `when:` skips the step with the failed condition as
the reason; `onFlowComplete` cleanup is isolated per command and reported as
`hook_failures` without ever masking the primary outcome. A suite runs on one
target: a flow whose `platforms` exclude it (declared for the other platform,
or an Android-only step on iOS) is skipped with a `flow_skipped_for_platform`
warning and listed under `skipped_flows` instead of aborting the rest, and a
suite of which no flow can run is refused with `flow_no_flows_found`. Only a
platform mismatch is skipped: a flow the target refuses for anything else — a
capability from `requires.capabilities` or `setup` it lacks
(`flow_requirements_unmet`) — still stops the suite. The
summary lists every run under `runs`; a single-file run carries the same
`runs` list (of one) next to its run's own keys.

`--until-step N` is prefix replay for debugging: it executes one flow through
runtime leaf step `N`, returns status `replayed`, skips `onFlowComplete`, and
leaves the target at that state. It reconstructs state from the flow start; it
does not claim that a native application snapshot exists. Prefix runs are
listed separately in suite totals and exported as skipped JUnit cases, never as
a complete test result. `--evidence
minimal|on-failure|always` overrides the header policy, and repeated `--collect
screenshot|hierarchy|logs|crashes|network` flags override its evidence kinds.

## What the language refuses, on purpose

- **YAML features beyond the subset**: anchors, aliases, tags, directives,
  block scalars, flow mappings, merge keys, tab indentation, duplicate keys,
  multi-line plain scalars. Each is a positioned `flow_parse_error` with a
  `reason` slug.
- **Fuzzy matching by default**: selectors default to `match: exact`
  (case-sensitive). `contains`, `regex`, and `caseInsensitiveExact` are
  opt-in per selector. A selector that matches more than one node refuses to
  act (`ambiguous_selector`) unless `index` disambiguates.
- **Unknown anything**: an unknown command, header field, selector field, or
  argument is an error, never ignored; a near miss gets a `Did you mean
  'tapOn'?` hint ahead of the full list of legal names. Deferred features
  (`waitForIdle`, `extendedWaitUntil`, `runScript`, `evalScript`) are
  rejected with a pointed hint.
- **Values that can only fail on the device**: a negative `timeoutMs`,
  `delayMs`, `durationMs`, `maxSwipes`, `chars`, or `quietMs`; a
  `waitForSettled` whose explicit `quietMs` exceeds its `timeoutMs`; a `setLocation`
  (or `setup.location`) latitude outside -90..90 or longitude outside
  -180..180; an empty selector string; a `match: regex` pattern that
  does not compile; and an `openLink` URL without a scheme (`myapp://path`,
  `https://host/path`) are positioned `flow check` errors (a pattern or URL
  that starts with `${VAR}` is checked when it runs). `timeoutMs: 0` is legal:
  one check, no waiting. A UTF-8 byte-order mark at the start of a file is
  accepted and ignored.
- **Type guessing**: `true` is a boolean only where a boolean belongs;
  a quoted `"true"` in a boolean slot is a positioned type error (so
  `text: No` never becomes `false`).

## Values and variables

Scalars are single-line: plain, `'single-quoted'` (`''` escapes a quote), or
`"double-quoted"` (`\\ \" \n \t \r \uXXXX`). `${NAME}` interpolates in any
scalar; `$${` is a literal `${`. Non-secret defaults live in the header
`env:`; secrets are passed at run time and never stored in the file.

**Run-scope variables** (no scripting involved): `copyTextFrom` reads the
matched node's text (falling back to its description; an empty read is a
test failure, `flow_copy_empty`) and `setClipboard` stores a literal — both
into `into: NAME` or the implicit `COPIED_TEXT`, which `pasteText` types.
Variables are global to the run, resolve after secrets and before `env`,
and may carry `sensitive: true` (the value is then redacted like secret
input). Order is checked **statically** before any device action: a
`${NAME}` used before its defining step, a name colliding with a declared
env/secret (`flow_var_conflict`), or a `pasteText` with nothing copied all
refuse at pre-flight. Definitions inside `repeat` or a `when:`-guarded
`runFlow` are not guaranteed to run and do not count outside them; cleanup
hooks see only what `onFlowStart` defined.

`requires.capabilities` is a frozen list (`ui.accessibility`, `screenshots`,
`logs`, `network.capture`). Unknown names are a header error. The runner
checks the resolved session before the first mutation and exits 2 with
`flow_requirements_unmet` when a declared facility is absent — for example
`network.capture` without an attached proxy, or `ui.accessibility` on iOS
without idb. `flow check` still validates names only; it does not probe
the host.

## Selectors

```yaml
selector:
  text: Continue        # visible text (iOS: prefer description — see below)
  match: exact          # exact | caseInsensitiveExact | contains | regex
  enabled: true
  index: 0              # only to disambiguate a justified duplicate
```

String fields: `id` (resource-id / accessibility identifier), `text`,
`visibleText`, `description`, `role`. State fields: `enabled`, `checked`,
`selected`, `focused`.

**`visibleText` is the cross-platform label field**: it matches the label a
user (or a screen reader) actually sees, wherever the platform stored it —
`text` on Android views, the accessibility label (`description`) on Flutter
and iOS. Prefer it for flows that must run on both platforms; `text` and
`description` stay strict, single-attribute matches for when the difference
matters. It is also the exact equivalent of Maestro's `text`, which matches
the same union — so an imported Maestro flow means on Autonom what it means
on Maestro.

**A selector only matches what is on screen.** A node that cannot be on
screen — iOS lists Flutter's scroll cache, laid out but not visible, at a 0x0
frame, and a node can lie entirely outside the app's frame — is never
selected by a flow: `assertVisible` does not pass on it, `tapOn` never taps
it, `scrollUntilVisible` keeps scrolling until the node is really on screen,
and `assertNotVisible` passes. Duplicates and `index` are counted among the
on-screen matches only, which is also how `autonom ui find` and `ui tap`
count first, so a selector unique on Android stays unique on iOS and a
`ui find --index N` names the node a flow's `index: N` does. `ui find --all`
still lists off-screen matches, last and marked `visible: false`.

**Whitespace is normalised** for `exact`, `caseInsensitiveExact`, and
`contains`: on both sides every run of spaces, newlines, or tabs compares as
one space and the ends are trimmed, so a merged Flutter label that iOS joins
with a newline (`M\nMarta`) and Android with a space (`M Marta`) is matched
by one selector. `regex` sees the raw value (a pattern can still target a
newline on purpose), and a selector made only of whitespace is compared raw.

Relational constraints narrow a match by another element (the *anchor*):

```yaml
selector:
  text: Settings
  match: exact
  leftOf:            # also: above, below, rightOf, childOf, containsChild,
    id: com.example.app:id/settings_secondary   # containsDescendants
```

Geometry is a pure edge comparison (no "nearest" guessing) and the anchor
of a geometric relation must match exactly one on-screen element; `childOf`
walks ancestors, `containsChild` checks direct children, and
`containsDescendants` checks any depth below, via the parent refs every
snapshot carries. Anchors identify by fields alone — no `index`, no nested
relations.

## Where flow files live

Flow files are **source** — they belong in your repository (the conventional
place is `.autonom/flows/` with shared steps in `.autonom/subflows/`).
Runtime artifacts never land there: runs write under
`~/.autonom/sessions/<id>/`. If your repository blanket-ignores `.autonom/`,
un-ignore the flows subtree (`!.autonom/flows/**`) or keep flows in another
directory — `flow check|fmt|list|run` take any path.

`runFlow` paths resolve relative to the referencing file, symlinks are
resolved, and the result must stay inside the workspace root (the nearest
ancestor of the root flow containing `.autonom`, else the root flow's own
directory). Recursion and cycles are refused with the full chain named and
the `file`/`line`/`column` of the `runFlow` step that closes the loop.

## Language surface

The block below is machine-checked against the command registry
(`tests/test_docs_flow_surface.py`) — a command or argument that exists but
is not listed here fails the build, and vice versa.

```text
header: schema appId name id description tags properties env requires sideEffects setup evidence onFlowStart onFlowComplete
selector-strings: id text visibleText description role
selector-bools: enabled checked selected focused
selector-relational: above below leftOf rightOf childOf containsChild containsDescendants
match-modes: exact caseInsensitiveExact contains regex
command launchApp: clearState resume label postcondition
command stopApp: label postcondition
command clearState: label postcondition
command openLink: url label postcondition
command tapOn: selector repeat delayMs label timeoutMs postcondition optional reason
command longPressOn: selector durationMs label timeoutMs postcondition optional reason
command doubleTapOn: selector label timeoutMs postcondition optional reason
command inputText: value sensitive requireFocus timeoutMs label postcondition
command eraseText: chars label postcondition
command pressKey: key label postcondition
command back: label postcondition
command swipe: direction from durationMs label postcondition
command scroll: label postcondition
command scrollUntilVisible: selector direction maxSwipes centerElement label postcondition
command copyTextFrom: selector into sensitive timeoutMs label
command setClipboard: value into sensitive label
command pasteText: label postcondition
command assertVisible: selector timeoutMs label
command assertNotVisible: selector timeoutMs label
command assertEnabled: selector timeoutMs label
command assertChecked: selector timeoutMs label
command waitUntil: visible notVisible timeoutMs label
command waitForSettled: timeoutMs quietMs label
command setLocation: latitude longitude label postcondition
command setPermissions: action service appId label postcondition
command addMedia: path label postcondition
command setOrientation: orientation label postcondition
command runFlow: file commands env when label
command repeat: commands times while label
command retry: commands maxAttempts onlyOn allowMutations label
command group: commands label
command takeScreenshot: label
command checkpoint: name
command note: text
deferred: waitForIdle extendedWaitUntil runScript evalScript
requires-capabilities: ui.accessibility ui.input screenshots screen.stream logs network.capture checkpoint.create checkpoint.restore simulator.location simulator.permissions simulator.clipboard simulator.appearance simulator.text_size simulator.status_bar simulator.battery simulator.network simulator.push simulator.sms simulator.call simulator.biometric simulator.keyboard
```

`sideEffects` declares the mutation classes a reviewer should expect:
`none`, `app-data`, `device-state`, `network`, `external-system`,
`credentials`, `media`, and `clipboard`. `none` cannot be combined with another
class.

`setup` is a strict mapping with `profile`, `fixtures`, `mocks`, `permissions`,
`location`, `orientation`, `appearance`, `locale`, `network`, and `reset`.
Provider-owned values are preflighted and applied before `onFlowStart`; external
App Skill/fixture selections remain visible as external instead of being
reported as successfully applied. The run manifest records available, selected,
applied, verified, and used setup entries separately.

## Semantics fixed by the language (run slice)

- Assertions **poll** — no hidden sleeps; the timeout is a *test failure*
  (exit 1, `failure_class: test_failure`), a dead backend is an
  *infrastructure error* (exit 2).
- Mutating commands (taps, input, links, device state) execute **exactly
  once** — never retried implicitly. `tapOn` with `repeat:` is N *declared*
  taps, not recovery.
- `inputText` first polls (up to `timeoutMs`, default 10 s) for a node that
  reports keyboard focus and fails with `flow_no_focused_field` (a test
  failure) when none appears: on real devices a `tapOn` that opens the
  field's activity followed by an immediate `inputText` typed into nothing
  and passed. An incomplete dump while the field's screen is still coming
  up is polled again, not an abort. iOS accessibility dumps carry no focus attribute (verified on
  a Simulator), so there the bar is "a text field is on screen".
  `requireFocus: false` opts out for a UI whose field never reports focus.
- `launchApp` starts the app **fresh**: the launcher activity on a cleared
  task on Android (`am start -W -f 0x10008000`, i.e. `NEW_TASK |
  CLEAR_TASK`), terminate-then-launch
  on iOS. Data is untouched (`clearState: true` wipes it). Measured on real
  devices, a resumed task put a flow's first selector on a subscreen — or,
  with Android Settings, in a search activity of another package that
  `stopApp` never touches. `resume: true` restores the old behaviour for a
  flow that continues a journey.
- `waitForSettled` polls the UI tree until it holds still — the same engine
  as `autonom ui wait --settled`: settled means consecutive snapshots with an
  identical tree for at least `quietMs` (default 500, capped at `timeoutMs`),
  bounded by `timeoutMs` (default 5000); `timeoutMs: 0` takes one snapshot,
  which cannot prove the screen still, so it reports `settled: false`. Put it
  before a tap on a sheet or page that is still
  animating in: measured on a device, a `tapOn` fired mid-animation missed.
  It never fails the flow — a tree still changing at `timeoutMs` (a spinner,
  a looping animation) is reported as `settled: false` on the step and a
  `screen_not_settled` run warning, and the flow goes on; assert the state
  you need with `waitUntil`. The warning says the tree was still changing
  only when the last snapshot was a change; when it changed and then held
  still for less than `quietMs` it says so, with how long it held; when
  fewer than two snapshots fit in `timeoutMs` (an Android dump can take
  ~2 s) it says settling could not be confirmed and how far to raise
  `timeoutMs`.
- `repeat` is bounded, declared iteration: `times` (1–25) is the hard
  limit, `while:` (`visible`/`notVisible` only) stops the loop early the
  moment it no longer holds; a failing iteration fails the flow, and
  composition (`runFlow`/`retry`/`group`/`repeat`) does not nest inside.
- `runFlow` with inline `commands:` runs an anonymous subflow: the parent
  frame stays visible and `env:` overlays it (a `file:` subflow starts
  from its own header env instead).
- `optional: true` exists only for external UI that does not define the
  scenario's success (the three tap commands only), always with a `reason:`;
  an optional assertion is a contradiction and is refused.
- `onFlowComplete` runs after pass *and* fail; a cleanup failure never masks
  the primary failure; evidence is captured before cleanup runs.
- `checkpoint` is an addressable replay boundary. Unless evidence mode is
  `minimal`, it captures the configured screenshot/hierarchy evidence even
  when the surrounding policy is `on-failure`.

## Recording a flow (Session → Flow)

`autonom flow create --from-session current --task login --out
.autonom/flows/auth/login.yaml` compiles a manual session into a validated
flow. The instrumented `ui tap|type|find` verbs record per-action detail
(matched node, proven selector, typed text) under the session's owner-only
`actions/`; the compiler reuses selectors that were proven unique during
the session, converts `ui type --sensitive` (and values typed into
credential-shaped fields) to `${SECRET_n}` placeholders that are never
stored, turns the final verifying `ui find` into the closing assertion,
and refuses to approximate what it cannot prove — coordinate taps and
point-to-point swipes are reported as warnings, not guessed. Journal argv is
read the way the CLI parser reads it: option values (`--serial S`,
`--label x`) are never taken for a positional, so `open <url> --serial S`
becomes `openLink: <url>`. `session launch` (which resumes the app) becomes
`launchApp: {resume: true}`; `session launch --fresh` becomes the bare, fresh
`launchApp`. What has no flow equivalent is skipped with a named warning
and counted in `quality.skipped`, never dropped: a `session uninstall`
(`uninstall_not_compilable`), an `open` URL without a scheme, which would not
pass `flow check` (`open_url_not_compilable`), the `--activity`, `--arg` and
`--setenv` of a `session launch` (`launch_args_not_compilable`; the launch
itself compiles, option values stay out of the report), and a `session clear`
recorded on iOS (`clear_state_not_compilable_on_ios` — `clearState` is
Android-only; declare `setup: {reset: true}` instead). The platform is the
journal entry's own `result.platform`, else the session's `platform`. The response
carries a quality report and the exact `flow run … --secret …` replay
command. End recordings with a `ui find` on the success state.

## Evidence reports

Every run writes `flows/<run_id>/manifest.json` (schema-versioned: status,
stable source/runtime step IDs, source position and redacted canonical args,
timestamps, matched target/bounds, before/after fingerprints, artifact links,
step-correlated scrubbed network previews, checkpoints, and reproduction
commands). `autonom report build` renders it as a fully
self-contained `report.html` (screenshots inlined as data: URIs, a CSP that
refuses any external fetch, everything escaped) plus a `report.xml` JUnit
file for CI; `report open` opens the HTML, `report export --format
html|junit --out` writes it anywhere. For a whole suite, `autonom
report suite` folds every run of the session into one `suite.html`
(totals, failures first, every flow expandable to its steps; failed
flows open by default) plus a `suite.xml` `<testsuites>` document —
the shape CI dashboards expect. It exits 1 when any flow failed. `--last N`
narrows it to the latest run of each of the N flows that ran most recently
(a flow is its header `id`, else its file): re-running one flow replaces that
flow's earlier run instead of pushing another flow out of the window, so
`--last 46` after a 46-flow suite plus one re-run still reports 46 flows. A run that
aborted on a definition or infrastructure error (exit 2 — an unreachable
`--until-step`, a dead backend) is a JUnit **error**, never a green suite: its
`<testsuite>` carries `errors="1"` and an `<error>` case built from the
primary error, a failed step of that class is an `<error>` rather than a
`<failure>`, and `suite.xml` rolls `errors` up next to `failures`.
`--detailed` turns that into a small site — `index.html` plus
`runs/<run_id>.html` per flow. Its addressable step timeline exposes the step
record, before/after frames with matched-target highlighting, UI hierarchy
diff, per-step device logs, and scrubbed network request/response previews.
Network capture that was not attached is shown as unavailable, never as an
empty request list. `--screenshots
none|failed|all` deciding whose frames are copied into `assets/`
(default `failed`: a full suite's frames run to hundreds of megabytes).
`--relative-to DIR` strips a local prefix so the whole site can be
committed and read from a repository. A failing step also leaves a
screenshot, a hierarchy dump, and a log window beside the events.

`autonom report serve --run <id> --open` binds only to `127.0.0.1` and adds a
`Replay to this step` control to the same report. The POST is protected by a
random page token and accepts only a run and step already present in the
session manifest; it cannot submit a command or arbitrary path. Replay uses
the flow's stored non-secret environment, requires secret values to be present
in the server process environment, collects detailed evidence, and links the
new replay run. Stop the foreground server with Ctrl-C.

## Proving a diff (PR Proof)

`autonom proof --base main` diffs the repository, selects the smallest
sufficient suite deterministically — a changed flow file selects itself, a
flow whose `properties.covers` globs (comma-separated, repo-relative) match
a changed file is selected, and `pull-request`-tagged flows always run —
executes it against the active session, and emits `proof.json` +
`proof.md` (one screen). Verdicts are fixed and never upgraded: `pass`
(exit 0), `fail` / `not_covered` (exit 1 — silence is not coverage),
`blocked` / `inconclusive` (exit 2). Changed files with no covering flow
are listed by name.

## The observed graph (Atlas-lite)

Every executed step's screen fingerprint rides in the events for free, and
`autonom atlas update` folds a session's runs (plus manually recorded tap
details) into `~/.autonom/apps/<app-id>/atlas/graph.json` — screens keyed by
a volatility-resistant structure hash (clock ticks, counters, list length,
and the status bar do not move it; a real state change becomes a *variant*),
transitions labeled by the triggering command with evidence references back
to the run and step. `atlas show|coverage|paths|export|diff` query it. The
graph records only what was observed: a missing edge means unknown, never
impossible.

## Maestro Core Profile

`autonom flow import maestro.yaml` converts a Maestro flow within the
documented Core Profile — header `appId`/`name`/`tags`/`env`/`properties`/
`onFlowStart`/`onFlowComplete`; `launchApp` (`clearState`), `stopApp`,
`clearState`, `tapOn`, `longPressOn`, `doubleTapOn`, `inputText`,
`eraseText` (`charactersToErase`), `pressKey`, `swipe` (direction),
`back`, `openLink` (link), `assertVisible`/`assertNotVisible`,
`extendedWaitUntil`→`waitUntil`, `waitForAnimationToEnd` (`timeout`)→
`waitForSettled` (both wait for a still screen and never fail),
`takeScreenshot`, `scrollUntilVisible`
(`element`/`direction`/`centerElement`), `retry` (`maxRetries`+1→
`maxAttempts`, capped at 3 attempts; mutating children get an explicit
`allowMutations: true` because that is what Maestro's retry does),
`runFlow` (`file` or inline `commands`, `env`, `when`), `copyTextFrom`/
`setClipboard`/`pasteText` (host-side variables — the OS clipboard is
untouched, exactly like Maestro), `repeat` (finite `times` required, ≤25;
`while` `visible`/`notVisible`; a JS `true:` refuses), `scroll`, `swipe`
`from:`, `tapOn` `repeat`/`delay`, selectors `text`/`id`/`index`/`enabled`
— into validated canonical Flow v1. Maestro's `text` imports as
**`visibleText`**, the same label union it matches upstream (text ∪
hintText ∪ accessibilityText), so a Flutter or iOS flow keeps finding its
elements instead of silently matching nothing. Import-only courtesies: single-line flow mappings
(`tapOn: {text: X, index: 1}` — the native grammar still refuses `{...}`),
and Maestro's on-selector `label`/`optional` move to the command
(`optional` on the tap commands only, with the generated
`reason: optional in the Maestro source`; an optional assertion refuses).
Maestro's regex-by-default matching is preserved honestly: a
metacharacter-free pattern becomes `match: exact`; a real pattern becomes
`match: regex` anchored as `^(?:...)$` (our regex is a search, Maestro's is
a full match). Everything outside the profile — scripts, JS interpolation,
point coordinates, random input — refuses with `unsupported_flow_command`
and the file position; an ambiguous conversion never produces a file that
silently means something else. `flow export --format maestro` goes the
other way, over a **narrower** surface than import: exact text is
regex-escaped, `label`/`optional`/`eraseText.chars` carry over,
`checkpoint`/`note` become comments, an assertion's `timeoutMs` becomes an
`extendedWaitUntil`, and a tap's `timeoutMs` (`tapOn`, `longPressOn`,
`doubleTapOn`) becomes `extendedWaitUntil: {visible: <selector>, timeout: N}`
followed by the tap — that wait is exactly what the timeout bounds. An
optional tap makes the wait `optional: true` as well; on import, an optional
`extendedWaitUntil` carrying only `visible`, `timeout` and `optional` right
before an optional tap on the same element folds back into that tap's
`timeoutMs`, and any other optional wait refuses (Autonom waits cannot be
optional, and a fold must not drop a `notVisible` arm or a label). `waitForSettled` exports as
`waitForAnimationToEnd` (`quietMs` has no Maestro form and refuses). Anything
Maestro cannot express identically — `inputText.timeoutMs`, relational
selectors, `group`, `setOrientation`, `assertEnabled`/`assertChecked`, and the
commands added in 0.28.1 (`scroll`, `repeat`, `swipe.from`, the clipboard
variables) — refuses rather than exporting something that means something
else. Note the one
asymmetry the format forces: our strict `text` exports as Maestro `text`,
which upstream matches the label union, so an exported flow can match
slightly more than the original; export `visibleText` when that matters.

A Maestro file also runs **directly**: `flow run|check|fmt|list` treat a
file whose header has no `schema:` field as a Maestro document and convert
it on the fly through the same importer — same refusals, nothing new to
learn. Nested `runFlow` children convert too, so a whole Maestro workspace
tree works. Converted runs carry `converted_from: maestro` in the
`flow.run.started` event and the run summary; `flow fmt` prints the
canonical Flow v1 text, which is also the migration path. `flow fmt
--write` never rewrites a Maestro source in place (`write_skipped` in the
entry says so) — converting to a file is always the explicit
`flow import --out`.

## Repairing a failed flow

Flows replay without a model, so when the app moves a button or renames a
label the run fails loudly and the YAML has to be fixed by hand. A *test*
failure in `flow run` therefore carries a `repair` block next to `failure`:

```json
"repair": {
  "step_index": 4, "command": "tapOn", "line": 12,
  "flow": "flows/login.yaml", "root_flow": "flows/smoke.yaml",
  "selector": {"text": "Sign In", "match": "exact"},
  "until_step": 2,
  "commands": [
    "autonom flow run flows/smoke.yaml --until-step 2 --secret PASSWORD --env USER=<value>",
    "autonom ui tree",
    "autonom ui find --text 'Sign In' --mode contains --all",
    "autonom screenshot --label 'repair tapOn line 12'",
    "autonom flow check flows/smoke.yaml",
    "autonom flow run flows/smoke.yaml --secret PASSWORD --env USER=<value>"
  ],
  "candidates": [
    {"selector": {"text": "Sign in", "role": "button"},
     "command": "autonom ui find --text 'Sign in' --role button --mode exact --case-sensitive",
     "score": 1.0, "matched_field": "text", "node": {"ref": "n7", "role": "button", "text": "Sign in"}}
  ],
  "advice": "The element the step targets was not on screen within timeoutMs. …",
  "evidence": "~/.autonom/sessions/<id>/flows/<run_id>/events.ndjson",
  "note": "The corrected flow is a reviewed edit, never an automatic rewrite."
}
```

The step is the one that **ended** the run — never a retry attempt that a
later attempt recovered, never a failing `onFlowComplete` cleanup. `flow`
is the file that contains it (a `runFlow` child when the failure happened
there; `line` is in that file), `root_flow` is what `flow run` replays.

The commands are the repair loop in order: replay the prefix so the device
sits in the state the failed step assumed, dump what is on screen now, query
the old selector *widened* (`contains`, `--all`) to see what it nearly
matched, keep a screenshot, then re-validate and re-run. `--until-step` is
the last step completed before the failure that is not one of its enclosing
blocks — for the first step inside a `runFlow`, that is the step before the
`runFlow`, not the block itself. When nothing ran before the failure the
replay command is omitted and `until_step_reason` says why. Every Flow
selector field maps to its `ui find` flag (`visibleText` becomes one query
per label source, `--text` and `--desc`); a `match: regex` selector keeps
`--mode regex`. Declared secrets and `--env` overrides appear as
`--secret NAME` / `--env NAME=<value>` placeholders — never their values.

When the failing step's hierarchy was captured, `candidates` ranks up to
five nodes that were on screen by similarity to the failed selector's text,
visibleText, description, and id (case-folded `difflib` ratio, threshold
0.5, a bonus when the role agrees). Each carries a ready-to-paste Flow
`selector` (with the `index` that picks it when its label is shared) and
the exact `ui find` query that confirms it. For `ambiguous_selector` the
list is every match instead, each with its `distinguishing` fields.

`advice` follows what flows actually produce: a selector that never matched
surfaces as `flow_assertion_timeout` (with the step's selector), plus
`ambiguous_selector`, `selector_index_out_of_range` (taps),
`coordinate_space_mismatch`, `flow_no_focused_field`, `flow_copy_empty`,
and `no_matching_node` (an off-screen relational anchor). Definition and
infrastructure failures abort with their own envelope and get no brief.
Nothing rewrites the flow: the candidates are suggestions, and the edit is
yours to review and commit.

## Errors

All Flow DSL codes are new and distinct from network capture's
`flow_not_found` (a recorded HTTP request — mitmproxy vocabulary — which that
subsystem keeps forever; see `docs/COMPATIBILITY.md`). Parse and validation
failures carry `file`, `line`, `column`, and `reason` extras. The variable
and iteration features add three stable codes: `flow_var_conflict` (an
`into:` name collides with a declared env/secret name anywhere in the flow
graph — static), `flow_repeat_invalid` (repeat bounds/nesting violations —
static), and `flow_copy_empty` (the matched node carries no text — a test
failure).
