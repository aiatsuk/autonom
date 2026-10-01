---
name: mobile-flow
description: Author, validate, and run repeatable Flow v1 files for AI agents — strict YAML flows with exact selectors, polling assertions, single-fire mutations, failure classes, and per-run event evidence via the Autonom CLI.
---

# Mobile Flow (Android + iOS Simulator)

## Purpose

Turn a working journey into a **repeatable, reviewable flow file** — and run
it with exact semantics. A flow is strict YAML in the app repository
(conventionally `.autonom/flows/`, shared steps in `.autonom/subflows/`);
runtime artifacts land under `~/.autonom/sessions/<id>/flows/<run_id>/`.

The language refuses what it cannot execute exactly: unknown commands,
fuzzy-by-default matching, YAML cleverness (anchors, block scalars, tabs),
and implicit retries of mutating actions. Every rejection carries
`file:line:column` and a stable `error_code`. Full reference: `docs/FLOW.md`.

## CLI

```bash
# Validate (whole runFlow graph, no device needed)
python3 <autonom-root>/scripts/autonom.py flow check .autonom/flows
python3 <autonom-root>/scripts/autonom.py flow check login.yaml

# Canonical form — expands shorthand, materializes `match: exact`
python3 <autonom-root>/scripts/autonom.py flow fmt login.yaml --write
python3 <autonom-root>/scripts/autonom.py flow fmt .autonom/flows --check   # exit 1 = needs formatting
python3 <autonom-root>/scripts/autonom.py flow fmt login.yaml --write --drop-comments   # only when losing comments is intended

# Enumerate: file, id, name, tags, platforms
python3 <autonom-root>/scripts/autonom.py flow list

# Run (needs an active session — see mobile-session)
python3 <autonom-root>/scripts/autonom.py flow run login.yaml
python3 <autonom-root>/scripts/autonom.py flow run .autonom/flows --include-tag smoke --exclude-tag flaky
python3 <autonom-root>/scripts/autonom.py flow run login.yaml --secret TEST_PASSWORD --env LOCALE=en_US
python3 <autonom-root>/scripts/autonom.py flow run login.yaml --events     # NDJSON stream on stdout
python3 <autonom-root>/scripts/autonom.py flow run login.yaml --dry-run    # pre-flight only; top-level status "planned", `planned` list with runtime indexes

# Approve for promotion: N consecutive clean replays of the current bytes
python3 <autonom-root>/scripts/autonom.py teach approve login.yaml --run --minimum-runs 3 --secret TEST_PASSWORD --env LOCALE=en_US
```

`flow check` catches, without a device: negative
`timeoutMs`/`delayMs`/`durationMs`/`maxSwipes`/`chars`, a latitude or
longitude out of range, an empty selector string, and a `match: regex`
pattern that does not compile (`timeoutMs: 0` is legal: one check). An
unknown command, argument, selector field, or match mode names its closest
legal spelling (`Did you mean 'tapOn'?`). `flow fmt --write` refuses with
`comments_would_be_lost` rather than silently dropping YAML comments; in a
directory nothing is written until every file is safe.

## A minimal flow

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

`flow fmt` expands `- tapOn: Sign in` into the explicit selector form with
`match: exact`. Selectors: `id`, `text`, `description`, `role`, plus
`enabled`/`checked`/`selected` and `index`. **iOS puts the visible label in
`description`, not `text`** — cross-platform flows prefer `id`, then
`description`.

## Reading a run

- Exit `0`: passed. Exit `1`: a **test failure** — the app did not do what
  the flow asserts; the stdout summary carries `status: "failed"` and
  `failure.failure_class: "test_failure"`. Exit `2`: the flow or the
  machine is wrong (definition/infrastructure) — stderr envelope as usual.
- Assertions poll (default 10 s, per-step `timeoutMs`); mutating steps fire
  **exactly once** — a duplicate selector match refuses instead of tapping.
- Selectors match **on-screen** nodes only, and duplicates and `index` are
  counted among them: iOS lists Flutter's scroll cache at a 0x0 frame, and a
  flow never asserts, taps, or counts such a node (`scrollUntilVisible` keeps
  scrolling until it is really on screen). Whitespace in a label is
  normalised, so one selector matches the label iOS joins with a newline and
  Android with a space. Before a tap on a sheet that is still animating in,
  add `waitForSettled` — it waits for a still tree and never fails the flow.
- A directory suite runs on one target and skips a flow that cannot run there
  (declared for the other platform, or an Android-only step on iOS) with a
  `flow_skipped_for_platform` warning; `flow list` shows each flow's
  inferred `platforms` up front.
- Per-step events: `~/.autonom/sessions/<id>/flows/<run_id>/events.ndjson`;
  a failing step leaves a screenshot and a hierarchy dump automatically.
- `onFlowComplete` cleanup always runs; its failures are reported separately
  (`hook_failures`) and never mask the primary outcome.
- A test failure also carries `repair`: the `--until-step` command that
  reconstructs the state the failed step assumed, `ui tree`, the old selector
  as a widened `ui find … --mode contains --all`, and the re-verification
  commands, with advice keyed by the error code. It names the step that
  ended the run (never a recovered retry or a cleanup hook), the file that
  holds it (`flow`; `root_flow` is what gets replayed), and `--secret NAME` /
  `--env NAME=<value>` placeholders — never values. When the failing step's
  hierarchy was captured, `candidates` ranks up to five on-screen nodes by
  similarity to the failed selector, each with a ready Flow `selector` and
  the `ui find` command that confirms it. Run them in order, confirm a
  candidate, edit the YAML, and re-run — the brief never rewrites the flow
  for you.
- A run that aborted on a definition or infrastructure error is a JUnit
  `<error>` (`errors="1"`), never a green suite.

## Rules

1. Never put credentials in a flow file. Pass `--secret NAME`; reference it
   as `${NAME}`. Values never enter the file, events, journal, or summary.
   An `--env` value that reaches a `sensitive: true` slot is treated as a
   secret too (redacted, reproduced as `--secret NAME`, and flagged with a
   `flow_env_value_sensitive` warning) — switch it to `--secret`.
2. Prefer `id`, then unique visible text (`description` on iOS). Use
   `index` only for a justified duplicate, never to paper over a bad
   selector.
3. Do not fight a `test_failure` by retrying the flow — read the failure
   evidence first; the app state, not the harness, is what changed.
   `launchApp` starts the app fresh (cleared task; `resume: true` to
   continue where it was), and `inputText` polls for a focused field before
   typing (`timeoutMs`), so a flow needs no hand-written waits for a resumed
   subscreen or a field that is not there yet.
4. Keep subflows atomic (login, dismiss-permissions) and let `runFlow`
   compose them; recursion and paths escaping the workspace are refused.
5. Approvals are bound to content. `teach approve` counts only replays whose
   recorded `flow_sha256` (and every `runFlow` child's `subflow_sha256`)
   matches the current bytes; the receipt stores both, and `app-skill
   promote` refuses a flow edited since. Edit, then replay again — an
   mtime-preserving copy does not count.
6. Flow files are source — commit them. If the repository blanket-ignores
   `.autonom/`, un-ignore the flows subtree (`!.autonom/flows/**`).

## Failure codes

| error_code | Meaning |
| --- | --- |
| `flow_parse_error` | YAML-subset violation; `file`/`line`/`column`/`reason` extras |
| `flow_unknown_command` / `flow_command_invalid` | not a v1 command / bad arguments |
| `flow_selector_invalid` | unknown or deferred selector field, bad match mode |
| `flow_file_not_found` / `flow_path_escapes_workspace` / `flow_cycle_detected` | runFlow graph problems |
| `flow_assertion_timeout` | test failure: the asserted state never held |
| `flow_no_focused_field` | test failure: `inputText` found nothing with keyboard focus to type into (`requireFocus: false` opts out) |
| `flow_var_undefined` / `flow_secret_undefined` | `${VAR}` unresolved / `--secret` not in the environment |
| `flow_no_flows_found` | no flow files (or none match the tag filters) |
| `flow_source_changed` | `teach approve` / `app-skill promote`: the flow or a `runFlow` child changed since its replays or receipt |
| `comments_would_be_lost` | `flow fmt --write` would drop comments; pass `--drop-comments` to accept that |
| `no_active_session` | run `session start` first (see mobile-session) |

## Related

- `mobile-session` — owning the device session a flow runs in.
- `mobile-screen` — exploring the UI to find stable selectors first.
- `mobile-memory` — prose runbooks; convert a stable runbook into a flow.
- `docs/FLOW.md` — the machine-checked language reference.
