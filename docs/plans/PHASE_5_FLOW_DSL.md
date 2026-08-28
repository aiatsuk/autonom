# Phase 5: Flow DSL v1 — strict repeatable flows

**Status:** implemented — all three slices (0.20.0 language, 0.20.1 executor, 0.20.2 composition) shipped as designed below.
**Goal:** give agents and humans one deterministic way to describe, validate,
and replay a user journey — readable YAML in the repository, exact semantics,
positioned errors, and evidence as part of the protocol.
**Depends on:** Phase 0 (`docs/plans/PHASE_0_RELEASE_ENGINEERING.md`) — CI and
test isolation land first, so every slice here ships against a trusted suite.
**Target versions:** | Version | Theme |
| --- | --- |
| 0.20.0 | Language + static tools: parser, schema, validator, canonical form; `flow check` / `fmt` / `list` |
| 0.20.1 | Executor core: `flow run <file>`, polling assertions, failure classes, events |
| 0.20.2 | Composition: runFlow execution, hooks, conditions, tags, suites; `mobile-flow` skill |

Version lanes 0.16–0.19 were reserved for the metrics phase while this phase
took 0.20.x (DEC-015). The reservation lapsed unused: the metrics phase
ultimately shipped after the whole flow arc as 0.27.0–0.27.2 (DEC-015 as
amended; mapping table in `docs/plans/PHASE_4_METRICS.md`).

---

## 0. Why this phase exists

Autonom can drive a device deterministically but every journey is improvised:
the journal records what an agent did, and nothing can replay it. Maestro
proves the flow-file UX; Revyl proves session→test→proof is the product loop.
Autonom's version must be stricter than Maestro where fuzziness caused real
harm (regex-by-default selectors, implicit mutation retries, a JS engine in
the YAML) and stay local-first where Revyl is cloud-bound.

### 0.1 Non-goals (explicit)

- No general-purpose programming: no JavaScript, no arbitrary HTTP, no
  unbounded loops, no `eval`.
- No full Maestro parity; import of a documented core profile is a later
  phase, and unsupported commands always fail loudly.
- No vision/LLM grounding in the executor — selectors are accessibility-first.
- No cloud orchestration; a flow file runs on the machine that has the target.
- No implicit recovery: a mutating command executes exactly once.

### 0.2 Design principles

- The language refuses what it cannot execute exactly (see `docs/FLOW.md`).
- Every failure is positioned (`file:line:column`) and classified
  (`test_failure` / `flow_definition` / `infrastructure`).
- The executor calls `autonom_lib` in-process — the flow engine is a consumer
  of the same library the CLI verbs use, never a shell-out wrapper.
- Evidence is written as structured events first; human renderings come later.

## 1. Language and static tools (0.20.0 — shipped)

The language reference and its machine-checked surface live in
`docs/FLOW.md`; the registry is `scripts/autonom_lib/flow/schema.py`.
Highlights: strict YAML subset with positioned single-code parse errors;
`match: exact` selector default; unknown anything is an error; typing is
positional (no Norway problem); `runFlow` graphs are statically loaded,
workspace-contained (symlinks resolved), and cycle-refused; `flow fmt` is
round-trip-safe and idempotent over the test corpus.

## 2. Executor design (0.20.1)

### 2.1 Verb

```bash
autonom flow run <file> [target flags] [--env K=V] [--secret NAME]
                 [--default-timeout-ms N] [--events] [--dry-run]
```

### 2.2 Semantics

- **Session-scoped:** requires an active session (`no_active_session` →
  infrastructure). Artifacts land under
  `~/.autonom/sessions/<id>/flows/<run_id>/`; the run never stops the session.
- **Pre-flight:** the typed model is walked against the resolved target
  before the first mutation — `clearState`/`back` on iOS, platform-invalid
  keys, and `requires.platform` / `requires.capabilities` mismatches fail
  with zero side effects; `--dry-run` stops here.
- **Polling engine:** assertions and `waitUntil` poll `ui.snapshot` on
  `time.monotonic` (default 10 s timeout, 500 ms interval, per-step
  `timeoutMs`, injectable clock in tests). Timeout →
  `flow_assertion_timeout`, **exit 1**, `failure_class: test_failure`.
  A backend error mid-poll aborts immediately as infrastructure (exit 2).
- **Single-fire mutations:** `tapOn` polls only while *zero* nodes match;
  the moment matches exist the selection rule applies once — ambiguity
  refuses (reusing `selector.select` verbatim), and the tap is never retried.
- **Double-dump fix:** `ui.tap` / `ui.swipe` gain a keyword-only `screen=`
  passthrough so the executor fetches the screen size once per run instead
  of per action (iOS `_guard_point` otherwise re-dumps the tree).
- **Events:** `events.ndjson` (chmod 600) with envelope
  `{schema_version, event_id, run_id, session_id, flow_id, timestamp, kind,
  platform, target_id, sensitive, payload}` (+ `serial` on Android, DEC-004);
  kinds `flow.run.started|step.started|step.finished|hook.finished|
  evidence.captured|run.finished`. The journal gets one slim `flow_step`
  line per step; `status` and `run_id` join `_SUMMARY_KEYS`. `--events`
  streams the NDJSON to stdout — a documented opt-in exception to the
  one-JSON-document rule; no repository gate uses it.
- **Secrets:** `--secret NAME` reads the process environment; values never
  enter events, journal, summary, or error messages; `sensitive: true`
  inputs are recorded as `<N chars>`.
- **Journal correctness fix:** `_journal_command` becomes exit-code-aware —
  today a handler returning nonzero is journaled `ok: true`; `flow run`'s
  exit-1 test failures make that bug visible, so it is fixed here.

### 2.3 Selector plumbing

`selector.py` gains `role` in `STRING_FIELDS` (additive; regression test for
the legacy `ui.find_nodes` path) and the CLI gains `--role`. The flow-side
mapping (`flow/selectors.py`): `id`→`resource_id`, `description`→`desc`,
`text`→`text` with the iOS caveat surfaced as a per-run warning event,
match modes `exact`→(exact, case-sensitive) etc. `focusable`/`scrollable`
stay out of the flow surface: iOS maps `AXFocused` into the compact node's
`focusable` while Android maps the real focusable attribute — a pre-existing
divergence to fix separately before either becomes a flow field.

## 3. Composition (0.20.2)

`runFlow` execution (child steps run by the same executor, `appId`
inherited, hooks not inherited); `onFlowStart`/`onFlowComplete` with
`session.run_teardown`-style isolation — cleanup failures recorded
separately, the primary failure never overwritten, evidence captured before
cleanup; `when:` conditions (platform / visible / notVisible / envEquals,
AND-only); optional steps honored at run time (skips reported with the
reason); `flow run <dir>` with `--include-tag` / `--exclude-tag`;
`scrollUntilVisible`, `assertEnabled`, `assertChecked`, `setLocation`,
`setPermissions`, `addMedia`; the `evidence:` policy honored for
screenshot+hierarchy capture. The CI emulator smoke gains a real
`flow run tests/fixtures/flows/settings_smoke.yaml` step. The `mobile-flow`
skill ships here, with the router/`autonom`/mobile-session/mobile-memory
skill updates and the docs sweep.

## 4. Error vocabulary

Shipped in 0.20.0 (`errors.py`, `# --- Flow DSL ---`): `flow_parse_error`,
`flow_schema_unsupported`, `flow_header_invalid`, `flow_unknown_command`,
`flow_command_invalid`, `flow_selector_invalid`,
`flow_optional_assertion_forbidden`, `flow_var_undefined` (run-time),
`flow_secret_undefined` (run-time), `flow_file_not_found`,
`flow_path_escapes_workspace`, `flow_cycle_detected`,
`flow_requirements_unmet` (run-time), `flow_assertion_timeout` (run-time),
`flow_check_failed`, `flow_no_flows_found`. Classes live in
`schema.failure_class()`; unknown codes classify as infrastructure — never
blame the app by default. `flow_not_found` remains network capture's forever
(`docs/COMPATIBILITY.md`).

## 5. Testing strategy

| Layer | What |
| --- | --- |
| Parser | corpus accept files + a rejection table asserting reason, line, column for every refused construct; deterministic mutation fuzz (never a traceback) |
| Canonical | fingerprint round-trip + text idempotence over the corpus; awkward-string quoting survives |
| Schema | registry integrity (mutating flag + slice per command), positional typing, optional/waitUntil/when rules, failure-class coverage of every flow code, a grep-guard that the DSL never mints `flow_not_found` |
| Validator | containment incl. symlink escape, cycle chains, cross-file attribution; CLI-level check/fmt/list behavior |
| Docs gates | `tests/test_docs_flow_surface.py` (registry ↔ `docs/FLOW.md`, both directions + meta-guard); the existing CLI-surface gate covers the verbs |
| Bare host | `flow check` succeeds tool-free on a valid file; missing paths fail with one code |
| Executor (0.20.1) | the login corpus flow green against fake Android *and* fake iOS; fake call-counts prove single-fire taps; injectable clock proves exit 1 vs exit 2; secrets absent from every artifact; contract probes with a hand-edited golden (`--write` stays forbidden) |
| Real device (0.20.2) | emulator CI job runs the settings smoke flow end to end |

## 6. Security

All existing invariants hold unchanged (consent cannot be granted from a
flow file; a flow containing a network step surfaces `consent_required` and
stops). New surfaces: runFlow containment via `Path.resolve()` against the
workspace root; events files chmod 600; interpolation never logs secret
values; imported/foreign YAML is refused at parse time rather than
sandboxed.

## 7. Decision log

| # | Decision |
| --- | --- |
| D1 | `contains` and `regex` are case-sensitive in flows; only `caseInsensitiveExact` is insensitive. |
| D2 | `flow fmt` never invents `label:` — labels are authorial; the executor derives display labels at run time. |
| D3 | Poll defaults: 10 000 ms timeout, 500 ms interval; a workspace config file is deferred. |
| D4 | Workspace root = nearest ancestor of the root flow containing `.autonom`, else the root flow's directory. |
| D5 | `inputText` carries no selector in v1 — focus is owned by the preceding `tapOn`, matching the platform reality that typing goes to the focused field. |
| D6 | `flow list` defaults to `.autonom/flows`. Flow files are source: repositories that blanket-ignore `.autonom/` should un-ignore the flows subtree (documented in `docs/FLOW.md`); this repository keeps its CI flows under `tests/fixtures/flows/`. |
| D7 | `optional:` is limited to `tapOn` in v1 and always requires `reason:`; optional assertions are refused. |
| D8 | mobile-memory's `~/.autonom/apps/<pkg>/flows/` prose-runbook store and Flow v1 files coexist: runbooks are working knowledge, flow files are repository source; converting a stable runbook into a flow is a Phase 6 (session→flow) concern. |

## 8. Open decisions (resolve during 0.20.1)

1. Whether `flow run --events` also mirrors events into the summary document
   or stays stream-only.
2. Whether `eraseText` on iOS (HID 42 per keypress) is honest enough to ship
   or should pre-flight-refuse until it can be proven on a real simulator —
   the fake proves dispatch, not effect (`docs/CAPABILITIES.md` records how
   that class of bug shipped before).

## 9. Related documents

- `docs/FLOW.md` — the language reference (machine-checked).
- `docs/COMPATIBILITY.md` — the contract the new exit code and
  `failure_class` field extend additively.
- `docs/AUTONOM_PRODUCT_RESEARCH_REVYL_MAESTRO.md` — the research this phase
  implements (its Phase 1).
