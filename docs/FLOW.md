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
```

`flow run` needs an active session (`autonom session start`), pre-flights the
whole flow against the resolved target before any mutation, and exits `0` on
pass, `1` on a *test failure* (summary on stdout with
`failure_class: test_failure`), `2` on definition/infrastructure errors
(stderr envelope). Events stream to
`~/.autonom/sessions/<id>/flows/<run_id>/events.ndjson` (or stdout with
`--events`); secrets pass via `--secret NAME` and never enter artifacts.
Directory runs execute a tag-filtered suite
(`flow run .autonom/flows --include-tag smoke --exclude-tag flaky`);
`runFlow` children execute inline with the root `appId` inherited and their
own env frame; a false `when:` skips the step with the failed condition as
the reason; `onFlowComplete` cleanup is isolated per command and reported as
`hook_failures` without ever masking the primary outcome.

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
  argument is an error, never ignored. Deferred features (relational
  selectors, `retry`, `waitForIdle`, script steps…) are rejected with a
  pointed hint.
- **Type guessing**: `true` is a boolean only where a boolean belongs;
  a quoted `"true"` in a boolean slot is a positioned type error (so
  `text: No` never becomes `false`).

## Values and variables

Scalars are single-line: plain, `'single-quoted'` (`''` escapes a quote), or
`"double-quoted"` (`\\ \" \n \t \r \uXXXX`). `${NAME}` interpolates in any
scalar; `$${` is a literal `${`. Non-secret defaults live in the header
`env:`; secrets are passed at run time and never stored in the file.

## Selectors

```yaml
selector:
  text: Continue        # visible text (iOS: prefer description — see below)
  match: exact          # exact | caseInsensitiveExact | contains | regex
  enabled: true
  index: 0              # only to disambiguate a justified duplicate
```

String fields: `id` (resource-id / accessibility identifier), `text`,
`description`, `role`. State fields: `enabled`, `checked`, `selected`,
`focused`. **iOS caveat:** UIKit/SwiftUI expose the visible label as
`description` (`AXLabel`), not `text` (`AXValue`) — cross-platform flows
should prefer `id`, then `description`.

Relational constraints narrow a match by another element (the *anchor*):

```yaml
selector:
  text: Settings
  match: exact
  leftOf:            # also: above, below, rightOf, childOf, containsChild
    id: com.example.app:id/settings_secondary
```

Geometry is a pure edge comparison (no "nearest" guessing) and the anchor
of a geometric relation must match exactly one on-screen element; `childOf`
walks ancestors and `containsChild` checks direct children via the parent
refs every snapshot carries. Anchors identify by fields alone — no `index`,
no nested relations.

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
directory). Recursion and cycles are refused with the full chain named.

## Language surface

The block below is machine-checked against the command registry
(`tests/test_docs_flow_surface.py`) — a command or argument that exists but
is not listed here fails the build, and vice versa.

```text
header: schema appId name id description tags properties env requires evidence onFlowStart onFlowComplete
selector-strings: id text description role
selector-bools: enabled checked selected focused
selector-relational: above below leftOf rightOf childOf containsChild
match-modes: exact caseInsensitiveExact contains regex
command launchApp: clearState label
command stopApp: label
command clearState: label
command openLink: url label
command tapOn: selector label timeoutMs optional reason
command longPressOn: selector durationMs label timeoutMs optional reason
command doubleTapOn: selector label timeoutMs optional reason
command inputText: value sensitive label
command eraseText: chars label
command pressKey: key label
command back: label
command swipe: direction durationMs label
command scrollUntilVisible: selector direction maxSwipes label
command assertVisible: selector timeoutMs label
command assertNotVisible: selector timeoutMs label
command assertEnabled: selector timeoutMs label
command assertChecked: selector timeoutMs label
command waitUntil: visible notVisible timeoutMs label
command setLocation: latitude longitude label
command setPermissions: action service appId label
command addMedia: path label
command setOrientation: orientation label
command runFlow: file env when label
command retry: commands maxAttempts onlyOn allowMutations label
command group: commands label
command takeScreenshot: label
command checkpoint: name
command note: text
deferred: waitForIdle extendedWaitUntil runScript evalScript repeat
```

## Semantics fixed by the language (run slice)

- Assertions **poll** — no hidden sleeps; the timeout is a *test failure*
  (exit 1, `failure_class: test_failure`), a dead backend is an
  *infrastructure error* (exit 2).
- Mutating commands (taps, input, links, device state) execute **exactly
  once** — never retried implicitly.
- `optional: true` exists only for external UI that does not define the
  scenario's success (`tapOn` only), always with a `reason:`; an optional
  assertion is a contradiction and is refused.
- `onFlowComplete` runs after pass *and* fail; a cleanup failure never masks
  the primary failure; evidence is captured before cleanup runs.

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
point-to-point swipes are reported as warnings, not guessed. The response
carries a quality report and the exact `flow run … --secret …` replay
command. End recordings with a `ui find` on the success state.

## Maestro Core Profile

`autonom flow import maestro.yaml` converts a Maestro flow within the
documented Core Profile — header `appId`/`name`/`tags`/`env`; `launchApp`
(`clearState`), `stopApp`, `clearState`, `tapOn`, `longPressOn`,
`doubleTapOn`, `inputText`, `eraseText`, `pressKey`, `swipe` (direction),
`back`, `openLink`, `assertVisible`/`assertNotVisible`,
`extendedWaitUntil`→`waitUntil`, `takeScreenshot`, `runFlow` (`file`, `env`,
`when`), selectors `text`/`id`/`index`/`enabled` — into validated canonical
Flow v1. Maestro's regex-by-default matching is preserved honestly: a
metacharacter-free pattern becomes `match: exact`; a real pattern becomes
`match: regex` anchored as `^(?:...)$` (our regex is a search, Maestro's is
a full match). Everything outside the profile — scripts, JS interpolation,
point coordinates, random input — refuses with `unsupported_flow_command`
and the file position; an ambiguous conversion never produces a file that
silently means something else. `flow export --format maestro` goes the
other way (exact text is regex-escaped; Autonom-only constructs refuse;
`checkpoint`/`note` become comments).

## Errors

All Flow DSL codes are new and distinct from network capture's
`flow_not_found` (a recorded HTTP request — mitmproxy vocabulary — which that
subsystem keeps forever; see `docs/COMPATIBILITY.md`). Parse and validation
failures carry `file`, `line`, `column`, and `reason` extras.
