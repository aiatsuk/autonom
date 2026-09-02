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

# Enumerate: file, id, name, tags, platforms
python3 <autonom-root>/scripts/autonom.py flow list

# Run (needs an active session — see mobile-session)
python3 <autonom-root>/scripts/autonom.py flow run login.yaml
python3 <autonom-root>/scripts/autonom.py flow run .autonom/flows --include-tag smoke --exclude-tag flaky
python3 <autonom-root>/scripts/autonom.py flow run login.yaml --secret TEST_PASSWORD --env LOCALE=en_US
python3 <autonom-root>/scripts/autonom.py flow run login.yaml --events     # NDJSON stream on stdout
python3 <autonom-root>/scripts/autonom.py flow run login.yaml --dry-run    # pre-flight only
```

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
- Per-step events: `~/.autonom/sessions/<id>/flows/<run_id>/events.ndjson`;
  a failing step leaves a screenshot and a hierarchy dump automatically.
- `onFlowComplete` cleanup always runs; its failures are reported separately
  (`hook_failures`) and never mask the primary outcome.
- A test failure also carries `repair`: the `--until-step` command that
  reconstructs the state the failed step assumed, `ui tree`, the old selector
  as a widened `ui find … --mode contains --all`, and the re-verification
  commands, with advice keyed by the error code. Run them in order, edit the
  YAML, and re-run — the brief never rewrites the flow for you.

## Rules

1. Never put credentials in a flow file. Pass `--secret NAME`; reference it
   as `${NAME}`. Values never enter the file, events, journal, or summary.
2. Prefer `id`, then unique visible text (`description` on iOS). Use
   `index` only for a justified duplicate, never to paper over a bad
   selector.
3. Do not fight a `test_failure` by retrying the flow — read the failure
   evidence first; the app state, not the harness, is what changed.
   Two first-step failures seen on real devices are authoring, not app,
   problems: `launchApp` resumes the app wherever it was (start with
   `stopApp`), and `inputText` right after the `tapOn` that opens a field
   finds nothing focused yet (add `waitUntil` on the field, or rely on the
   built-in focus poll and its `timeoutMs`).
4. Keep subflows atomic (login, dismiss-permissions) and let `runFlow`
   compose them; recursion and paths escaping the workspace are refused.
5. Flow files are source — commit them. If the repository blanket-ignores
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
| `no_active_session` | run `session start` first (see mobile-session) |

## Related

- `mobile-session` — owning the device session a flow runs in.
- `mobile-screen` — exploring the UI to find stable selectors first.
- `mobile-memory` — prose runbooks; convert a stable runbook into a flow.
- `docs/FLOW.md` — the machine-checked language reference.
