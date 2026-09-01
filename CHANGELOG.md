# Changelog

All notable changes to Autonom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as enforced by `scripts/validate_plugin.py` (the library version in
`scripts/autonom_lib/__init__.py` is the single source of truth).

## [Unreleased]

Deterministic capture state and a repair hand-off, borrowed from
[goldie](https://github.com/kacperkapusciak/goldie) — an App Store screenshot
generator that replays flows on the same simulators and emulators and had
already solved the noise that makes two captures of one screen differ.

### Added
- **`simulator status-bar pin`** on both platforms: 9:41, full battery and
  signal, no notifications — `simctl status_bar override` on iOS, SystemUI
  demo mode on Android — so a before/after screenshot diff shows only what
  the app changed. Given keys override the preset; `clear` restores the bar.
- **`simulator keyboard pin|reset|show`** (iOS): autocorrect, prediction, and
  auto-capitalisation off and the locale set, written with `plistlib` into
  the shut-down simulator's preference store and read back for `verified`.
  `reboot=true` cycles a booted device; a booted one without it is refused
  with the new `simulator_must_be_shutdown`; a device with no data directory
  with `simulator_data_not_found`. Android refuses with
  `unsupported_capability` because the settings live inside Gboard.
  `AUTONOM_CORESIMULATOR_DEVICES` relocates the Devices directory.
- **Flow repair brief:** a test failure in `flow run` carries `repair` — the
  `--until-step` prefix replay, `ui tree`, the old selector as a widened
  `ui find … --mode contains --all`, a labelled screenshot, and the
  re-verification commands, plus advice keyed by the error code and the
  events path as evidence. Definition/infrastructure failures get none.
- `screenshot`, `shots show`, and the shot index report `width`/`height`
  from the PNG header, so an agent knows the capture's coordinate space.
- `devices` adds `avd_profiles` (hardware profile, screen size and density,
  API level, ABI from each AVD's ini files; nulls when unreadable) and names
  the `avd` a running emulator booted from via its console.
- `doctor` reports every active `AUTONOM_*` override under `overrides` and
  warns with `override_path_missing` when a binary override points at
  nothing — the stale-environment trap that made a present tool read as
  missing.

### Changed
- `simulator status-bar override` on Android now enters demo mode explicitly
  and accepts `battery`, `plugged`, `wifi`, `wifi_level`, `mobile`,
  `mobile_level`, `datatype`, and `notifications` alongside `hhmm`; an unknown
  key is refused before anything is broadcast.
- The fake `simctl` now moves a device to `Shutdown` on `shutdown`, so a
  shutdown/write/boot sequence is proven by the device list.

## [0.30.0] - 2026-08-28

Autonom now implements the end-to-end product blueprint: strict portable
flows, semantic provider preflight, teaching and reusable app skills,
continuous step evidence, immutable report bundles, deterministic replay,
campaign-level CI, and one Android/iOS Mobile Canvas control plane.

### Added
- **Report Model v2 and immutable bundles:** canonical run, case, attempt,
  action, artifact, environment, setup, and replay records; content-addressed
  blobs; integrity verification; separate mutable annotations; complete
  per-step screenshot, hierarchy, log, and request deltas.
- **First-class exports and gates:** Allure 3 result files, compact agent JSON,
  JUnit, CSV, metrics, explicit gate rules, retry/flaky history, supervised CI
  shards, deterministic pack/merge/finalize, and independent publication.
- **Teach and App Skills:** marked journal ranges compile into reviewed flows;
  promotion requires three clean replays and stores only the fixed portable
  `.autonom/apps/<app-id>` contract.
- **Provider and setup contracts:** immutable semantic capability snapshots,
  preflight before mutation, a recorded Setup Catalog, explicit side effects,
  postconditions, simulator controls, and typed unsupported combinations.
- **Unified Mobile Canvas:** authenticated browser bootstrap, HttpOnly session
  cookies, CSRF checks, persistent journaled input, Android H.264 streaming,
  screenshot fallback, iOS Simulator point/pixel mapping, and explicit
  human/agent/replay control ownership.
- **Portable replay and Runtime Map:** bundle-contained flow graphs, replay to
  a stable step or checkpoint, and a descriptive `runtime-map` alias for the
  observed-only Atlas graph.

### Changed
- The event stream uses the ordered `autonom.event/v1` envelope with stable
  identity, monotonic and wall clocks, origin, attempt linkage, and
  pre-persistence redaction.
- The HTML evidence report has keyboard navigation, filtering, stable step
  anchors, first-causal-failure links, setup/capability inspection, and clear
  separation between execution status and proof verdict.

## [0.29.0] - 2026-08-28

The evidence report now implements the step-level debugging loop promised by
the product roadmap.

### Added
- **Manifest v3 step records:** stable source/runtime IDs, source columns,
  redacted canonical arguments, start/end timestamps, matched accessibility
  target bounds, pre/postcondition fingerprints, checkpoint indexes, exact
  execution commands, and step-correlated scrubbed network previews.
- **Addressable evidence UI:** every step has a direct timeline anchor and
  panels for before/after screenshots, matched-target highlighting, UI
  hierarchy diff, device logs, and request/response previews. An unattached
  network capture is explicitly unavailable instead of looking like an empty
  request list.
- **Portable prefix replay:** `flow run --until-step N` reconstructs state
  from the flow start, stops after the selected runtime step with status
  `replayed`, and skips cleanup hooks so the state remains inspectable.
  `--evidence` and repeatable `--collect` flags control the replay bundle.
- **Local report controls:** `report serve` binds to loopback and adds a
  token-protected replay button to each recorded step. Browser input is
  restricted to an existing run and step; it cannot provide a command or
  arbitrary path.
- `checkpoint` now captures its configured screenshot and hierarchy evidence
  and is recorded as an addressable replay boundary.

### Changed
- Evidence mode `always` captures both sides of every step so screenshot and
  hierarchy comparisons have a real before/after pair.
- Network records include millisecond boundaries for honest step correlation;
  headers and bodies remain scrubbed before they reach disk or the report.
- Prefix replay runs are reported separately and do not fail a session suite.

## [0.28.5] - 2026-08-20

Codex review of PR #1: three remaining findings, each pinned by a regression.

### Fixed
- **`when.envEquals` skip reasons leaked `--secret` values.** A mismatched
  condition printed both sides verbatim into events, the manifest, HTML, and
  JUnit. The reason now names the variable only when either side is a secret
  or a `sensitive:` runtime value.
- **Recovered `retry` attempts failed CI JUnit.** The executor keeps every
  attempt in `manifest.steps`; `report export --format junit` (and the suite
  document) counted those retained failures. Superseded attempts are now
  `<skipped message="retried"/>` and do not increment `failures`.
- **`requires.capabilities` was accepted and ignored.** The schema now
  freezes the research vocabulary (`ui.accessibility`, `screenshots`,
  `logs`, `network.capture`); preflight raises `flow_requirements_unmet`
  before the first mutation when the session cannot provide a declared
  facility.

## [0.28.4] - 2026-08-17

Manifest v2 and the evidence-integrity fixes an adversarial validation of the
whole 0.28.x arc turned up. The manifest is the report protocol, and it was
missing most of what a real report needs.

### Added
- **Manifest `schema_version: 2`** (additive; v1 manifests still render):
  wall-clock `started_at_ms`/`finished_at_ms` on the run and every step, the
  `selector` actually used per step, block spans for `group`/`repeat`/`retry`/
  `runFlow` (`blocks`, with iteration and attempt ranges, kept out of `steps`
  so JUnit keeps counting the same cases), `depth`/`parent_index`/
  `retry_attempt`, flow metadata (`tags`, `properties`, `description`, `env`,
  `secret_names`, `converted_from`, `workspace_root`, `evidence_mode`), and
  `artifact_steps` — the authoritative artifact→step ledger.

### Fixed
- **Reports called deliberate skips failures.** An `optional: true` step keeps
  the error it tolerated, and both suite renderers printed that error in
  failure red regardless of the step's status — a skipped step looked like a
  defect on every page. The error block is now gated on `status: failed`;
  a skip shows its reason with the tolerated code as muted text.
- **A frame too large to inline silently disappeared** from the single-run
  report (`_MAX_INLINE_IMAGE`, 2 MB — an ordinary 1080×2400 emulator frame
  exceeds it): the report then read as "nothing was captured". Oversized
  frames now render a placeholder naming the file and its size.
- **The sensitive guardrail stopped at the single-run report.** The suite and
  per-flow pages never looked at `sensitive`, and their assets were written
  0644. Both now carry the ⚠ banner, and a sensitive run keeps the whole
  output tree owner-only.
- `suite.xml` was hard-coded 0600 while everything around it was 0644 — the
  one file CI actually consumes was the one CI could not read.
- `report suite --detailed` never pruned its output: `runs/` and `assets/`
  from an earlier, different run survived into the new site and read as
  current evidence. Both are rebuilt from scratch (inside `--out` only).
- **`flow export --format maestro` dropped arguments silently** — `optional`,
  `reason`, `label` and `eraseText.chars` vanished, directly contradicting the
  profile's "convert faithfully or refuse" contract. They now carry over
  (Maestro has equivalents for all of them), and a per-command `timeoutMs`,
  which Maestro cannot express, refuses with a pointer to `extendedWaitUntil`.
  The refusal hint no longer claims `retry`/`scrollUntilVisible` are
  Autonom-only — both are Maestro commands that this release imports.
- An `AutonomError` raised while **evaluating** a `runFlow when:` or a
  `repeat while:` clause produced no step outcome at all: it unwound past the
  timeline, so the manifest showed no failing step. The failure now lands on
  the step that owns the condition.
- **An aborted run left no evidence at all.** An infrastructure or
  flow-definition failure raised straight out of `run()`, so `_write_manifest`
  never executed: the one run a human most needs to inspect had no manifest and
  no report. The error is now recorded (status, `primary_error`), the manifest
  is written, and the envelope still reaches the CLI unchanged (exit 2).
- **`takeScreenshot` frames were invisible in the suite site.** They carry a
  user label, not a `step-N` name, so the renderer filed them under step 0 —
  a bucket no step ever has — copying megabytes to disk that no page
  referenced. Frames are now mapped through the manifest ledger; the executor
  records a step for every capture, including `takeScreenshot`, which
  previously emitted no evidence event at all.
- **A screenshot label could impersonate a step number.** `takeScreenshot:
  step-1-decoy` filed its frame under step 1 and rendered it beside an
  unrelated command. Filenames are no longer parsed for step numbers.
- `report suite --detailed` was a no-op: the multi-page site was built
  whenever `--screenshots` was not `none` (its default is `failed`), so the
  documented single-page default never existed. The flag now gates the site.
- A malformed `match: regex` no longer reads as a clean negative in
  assertions (`select_all` shared `no_matching_node` with "invalid regular
  expression"), and a bad regex inside a **relational anchor** raises the
  positioned envelope instead of a raw `re.error` traceback.

## [0.28.3] - 2026-08-17

Suite-level evidence: running 46 flows produced 46 separate reports and no
way to see the run as a whole. Found by dogfooding a real app.

### Added
- `autonom report suite [--session ID] [--last N] [--out DIR] [--open]` —
  one page over every flow run in the session: totals (flows / passed /
  failed / total step time), a failures-first list, then every flow as an
  expandable block with its steps, durations and reproduction command
  (failed flows expanded by default). Same containment rules as the
  per-run report: no external fetch, everything escaped. Screenshots stay
  in the per-run reports — inlining dozens of runs would produce a
  multi-hundred-megabyte page.
- The same command writes `suite.xml`, a single JUnit `<testsuites>`
  document with one `<testsuite>` per flow — the shape CI dashboards
  expect. Exits 1 when any flow failed, 0 otherwise.
- `report suite --relative-to DIR` strips that prefix from flow paths and
  reproduction commands, so a report committed to a repository carries
  repo-relative paths (`autonom flow run .autonom/flows/…`) instead of one
  machine's home directory — found while committing a real report.
- `report suite --detailed` writes a small static site instead of a single
  page: `index.html` linking to `runs/<run_id>.html` per flow, each step
  with its screenshots, the device-log window captured at the failure and a
  link to the hierarchy dump. `--screenshots none|failed|all` controls whose
  frames are copied into `assets/` (default `failed`) — a whole suite's
  frames are ~100 MB, which is why nothing is inlined as base64 here.

## [0.28.2] - 2026-08-17

Selector fidelity for Maestro import, found by running a real Maestro flow
against a real Flutter app: the imported file executed but matched nothing.

### Added
- New selector field **`visibleText`** — the label a user or screen reader
  sees, wherever the platform stored it (`text` on Android views, the
  accessibility label on Flutter/iOS). One cross-platform field for flows
  that must run on both, while `text`/`description` stay strict
  single-attribute matches.

### Fixed
- **Maestro's `text` now imports as `visibleText`, not `text`.** Upstream,
  `text` matches the union of text / hintText / accessibilityText
  (`Filters.kt`); importing it as our strict `text` attribute meant every
  Flutter and iOS flow converted cleanly and then matched nothing —
  precisely the "parses but means something else" failure the profile
  exists to prevent. Verified end to end: a hand-written Maestro flow that
  switches the app language now passes unchanged through `flow run`.
  `flow export` maps `visibleText` back to Maestro `text`.

## [0.28.1] - 2026-08-17

Maestro Core Profile v2, slice 2 of 4: engine-only commands — value
extraction without a script engine, bounded iteration, and composition
completion. No new device substrate; everything rides on the existing
selector engine and gesture paths.

### Added
- **Run-scope variables, no JS**: `copyTextFrom` (selector → node text,
  description fallback; empty read = new test-failure code
  `flow_copy_empty`), `setClipboard` (literal), both into `into: NAME` or
  the implicit `COPIED_TEXT`; `pasteText` types it. Host-side only — the OS
  clipboard is untouched (exactly Maestro's semantics). Precedence env <
  variable < secret; `sensitive: true` redacts like secret input.
  Pre-flight is order-aware: use-before-definition, a name colliding with
  env/secrets (new code `flow_var_conflict`), and `pasteText` with nothing
  copied refuse statically; definitions inside `repeat` or a `when:`-guarded
  `runFlow` do not escape, and cleanup hooks see only `onFlowStart`
  definitions.
- **Bounded `repeat`** (leaves the deferred list): mandatory `times` 1–25,
  `while:` (`visible`/`notVisible`) checked before each iteration, per-block
  `iterations`/`stop_reason` in events; composition does not nest inside;
  violations are the new `flow_repeat_invalid`. No `allowMutations` gate —
  declared iteration is not failure recovery.
- **Composition completion**: `runFlow` accepts inline `commands:`
  (anonymous subflow; parent frame visible, `env:` overlays); `swipe`
  accepts `from: <selector>` (anchored at the element's center, clamped);
  `scroll` (one upward swipe); `scrollUntilVisible.centerElement` (≤3
  corrective micro-swipes along the scroll axis); `tapOn.repeat`/`delayMs`
  (2–10 declared taps); selector relation `containsDescendants` (any-depth,
  leaves the deferred fields).
- **Import widened accordingly**: the clipboard trio, `scroll`, bounded
  `repeat` (unbounded/JS `while` refuses), inline `runFlow.commands`,
  `swipe.from`, `scrollUntilVisible.centerElement`, `tapOn` `repeat`/`delay`
  all convert from Maestro files; their `_UNSUPPORTED_HINTS` entries are
  gone.

### Fixed
Hardening from the slice's adversarial review, each pinned by a test:
- **Relational selector constraints were silently dropped in every
  assertion/condition path** (pre-existing since 0.20.1): `assertVisible`/
  `assertNotVisible`/`waitUntil`/`scrollUntilVisible` and `when:` conditions
  matched on plain fields only, so an impossible relation could produce a
  false PASS (and a true one a false FAIL). All matching now routes through
  one relations-aware selection (`flow/selectors.select_all`); in an
  assertion context an absent geometric anchor means "not present", never an
  error.
- A `runFlow` file reference nested inside `group` escaped both walkers —
  `flow check` skipped its existence/cycle/containment checks and `flow run`
  crashed with a raw KeyError; both walkers now descend into every nested
  command list.
- Variable-name conflicts are checked against every name declared anywhere
  in the flow graph (child header envs, `runFlow env:` overlays), so a
  runtime variable can never silently shadow a subflow's own env; an inline
  `runFlow env:` can no longer shadow a secret; `pasteText` honors an
  env-declared `COPIED_TEXT`; a pre-flight memo hit still contributes the
  subflow's definitions (valid flows were refused); cleanup-hook definitions
  no longer leak between failure-isolated cleanup steps.
- `scrollUntilVisible` + `centerElement` no longer crashes with a raw
  IndexError when relations filter everything out, and its ambiguity
  semantics are explicit: centering needs exactly one match
  (`ambiguous_selector` otherwise, including mid-centering).
- The run-level `sensitive` flag now sees `sensitive: true` in hooks, nested
  command lists, and subflows — not just top-level root steps.
- `_swipe_from`/`_center_node` recover from a mid-flow rotation
  (COORDINATE_SPACE_MISMATCH refresh-and-retry) like `_tap`/`_swipe` do.
- Export refuses the new inline `runFlow.commands`, `tapOn.repeat`, and
  `swipe.from` instead of crashing (KeyError) or silently dropping arguments;
  `retry` refuses `repeat` inside (it hid mutating children from the
  `allowMutations` scan).
- The canonical emitter wrote every `when`-kind argument as `when:` — a
  `repeat.while` would have round-tripped as `when` (caught by the new
  round-trip tests before it could ship).

## [0.28.0] - 2026-08-17

Maestro Core Profile v2, slice 1 of 4 (`docs/plans/PHASE_6_MAESTRO_COMPAT.md`):
import courtesy. A real-world Maestro file now runs through Autonom without a
separate conversion step, and the most common idioms that used to die as raw
parse errors import cleanly — while the native Flow v1 grammar stays exactly
as strict as before.

### Added
- The strict parser accepts single-line flow mappings
  (`tapOn: {text: X, index: 1}`) in an opt-in mode used only by the Maestro
  importer — flat, scalar values only, every malformed shape a positioned
  `flow_parse_error` (new reason slugs `nested_flow_mapping`,
  `unterminated_flow_mapping`). Hand-written Flow v1 files still refuse
  `{...}` with reason `flow_mapping`.
- `flow run|check|fmt|list` execute Maestro files directly: a flow file whose
  header has no `schema:` field is converted on the fly through the importer
  (decision D6) — same refusals as `flow import`, nested `runFlow` children
  convert too. Converted runs carry `converted_from: maestro` in
  `flow.run.started` and the run summary; `flow fmt` prints the canonical
  Flow v1 text as the migration path — and `flow fmt --write` never rewrites
  a Maestro source in place (the entry reports `converted_from` +
  `write_skipped`; conversion to a file is always the explicit
  `flow import --out`).
- Import profile widened: header `properties`/`onFlowStart`/`onFlowComplete`
  (`url` refuses — no web target); map forms of `inputText` (`text`),
  `eraseText` (`charactersToErase`), `openLink` (`link`), `takeScreenshot`
  (`path`); `scrollUntilVisible` (`element`/`direction`; time/speed tunables
  refuse toward `maxSwipes`); `retry` (`maxRetries`+1→`maxAttempts`, capped
  at 3 attempts total, mutating children get an explicit
  `allowMutations: true` because that is Maestro's semantics); Maestro's
  on-selector `label`/`optional` move to the command (`optional` on tap
  commands only, with a generated `reason:`; an optional assertion refuses);
  `label` imports on every mapped command.

### Fixed
Hardening from the slice's adversarial review, each pinned by a test:
- Non-integer values where the importer expects a number (`swipe.duration`,
  `extendedWaitUntil.timeout`, `eraseText`, selector `index`) refuse with a
  positioned `unsupported_flow_command` instead of an uncaught traceback —
  and so does every malformed value shape (`properties: oops`, `env: oops`,
  a list under `retry`/`swipe`/`tapOn`, a scalar under `extendedWaitUntil`),
  which previously crashed with AttributeError and, through the auto-detect
  loader, could kill a whole `flow check <dir>` sweep.
- Maestro's YAML 1.1 boolean spellings (`True`, `yes`, `on`, …) normalize on
  import for `optional`/`enabled`/`clearState`; unrecognized spellings
  refuse. Previously `optional: True` silently imported as *not* optional.
- Convertible-but-invalid constructs (nested retry, negative `maxRetries`,
  empty `commands`, state-only selectors, malformed env names) refuse at
  their position in the *source* file; a validation error escaping the
  canonical rebuild is reported as a conversion failure instead of
  presenting canonical-text coordinates as source positions.
- Flow-mapping values keep mid-word apostrophes plain (`{text: Don't
  allow}`) — a quote is a quote only at value start, as in YAML.
- `docs/FLOW.md` prose caught up with the shipped surface: relational
  selectors and `retry` are implemented (not "deferred"), and `optional`
  applies to all three tap commands, not `tapOn` alone.

## [0.27.3] - 2026-08-15

### Fixed
Findings of the closing adversarial review of 0.26.0–0.27.2, each pinned
by a regression test:

- iOS pid resolution dropped its `pgrep -f <bundle-id>` fallback: the
  CLI's own command line contains the bundle id (`--app-id …`), so a dead
  app could "resolve" to the autonom process itself and `metrics
  snapshot` would silently measure the wrong program. A missing pid is
  now always an `app_not_running` refusal.
- Same-second metrics artifacts no longer overwrite each other: all
  writers share one naming owner (`metrics/artifacts.py` — `{stamp}-
  {label}` order everywhere, 0600, `-2`/`-3` suffixes on collision), so
  `metrics series --interval 0` keeps every sample. A snapshot's raw
  dump is now `…-meminfo.raw.txt` so `metrics memory analyze` (glob
  `*-meminfo.txt`) can never fold snapshots into a capture-pack series.
- A consumer closing the NDJSON pipe (`… follow | head`) ends the stream
  cleanly (exit 0) instead of a BrokenPipeError traceback; stalled
  device calls (`dumpheap`, simpleperf, a wedged adb) fail as one
  `backend_failed` envelope instead of an uncaught TimeoutExpired.
- `follow` reads files in binary with exact byte offsets: text-mode
  `tell()` returns an opaque cookie mid-multibyte-character, which could
  misread rotation and replay the whole file; reads are also bounded
  (1 MiB slices) and `follow_poll` never sleeps past `--max-seconds`.
  `--grep` is now case-insensitive like `logs tail`/`journal --grep`;
  a process stream's final unterminated line is emitted, not dropped.
- iOS `logs follow --source device` honors `--session-id` (replays that
  session's recorded stream, refuses if none), applies `--package` when
  tailing a stream file, and falls back to live `log stream` when the
  recorded file's writer is dead — a stale file no longer shadows live
  logs.
- `autonom proof` shares `--env`/`--secret` handling with `flow run`: a
  malformed `--env` is a hard `flow_command_invalid` refusal instead of
  a silently dropped override that could pass a diff under the wrong
  config. `parse_cpuinfo` can no longer credit `com.example.app` with
  `com.example.app.dev`'s CPU line. `metrics memory capture` warns when
  gfxinfo fails instead of silently omitting the artifact; `metrics
  memory analyze` without a session hints at `--dir` (the flag it
  actually has); Flutter frame-key preference is deterministic; `metrics
  frames reset` joined the bare-host sweep.

## [0.27.2] - 2026-08-15

### Added
- Metrics depth (research Phase 4 §2.4–2.6), closing the metrics phase:
  `metrics memory capture` writes the Android evidence pack (metadata,
  meminfo, proc status, gfxinfo, optional HPROF with `--no-hprof` for
  non-debuggable builds; the remote dump is always cleaned up) and
  `metrics memory analyze` runs the series math over captured meminfo
  files. `metrics memory warn` posts the simulated memory-warning
  notification on the iOS Simulator (best-effort, and says so).
- `metrics frames reset|capture` wraps gfxinfo with a best-effort summary
  (unknown API shapes stay honest: raw artifact + `parsed: false`);
  `metrics frames flutter-summary` is the CLI twin of the
  flutter-performance-audit script, pinned by an equivalence test.
- `metrics trace --preset simpleperf|gfxinfo-flow|allocations|
  time-profiler|leaks|hitches` records heavy profiles with an explicit
  duration into session `metrics/` — missing tools are `tool_missing` with
  an install hint, platform mismatches `preset_unavailable`, profiler
  failures `trace_failed` with the stderr tail. xctrace argv is fake-
  tested; real-Xcode recording remains a manual checklist item (⚠️ in
  CAPABILITIES).
- Skills now lead with the CLI (`android-memory-leaks`,
  `android-runtime-performance`, `flutter-performance-audit`,
  `flutter-memory-leaks`, `ios-debugger-agent`, `mobile-session`,
  `autonom`); the standalone scripts remain for hosts without a session.
- `docs/plans/PHASE_4_METRICS.md` renumbered to the shipped 0.27.0–0.27.2
  lanes (DEC-015 amended): the 0.16–0.19 reservation lapsed unused.

## [0.27.1] - 2026-08-15

### Added
- Metrics foundation (research Phase 4 §2.2–2.3, §2.7). `metrics snapshot`
  answers "how is the app right now": Android reads `dumpsys meminfo` +
  `/proc/<pid>/status` + best-effort `cpuinfo`; iOS measures the Simulator
  process **on the host** (`ps` RSS/CPU + data-container size) and carries
  `metric_semantics` + `limitations` saying exactly that — the two are
  never comparable 1:1. Partial data prefers `ok: true` with `warnings[]`.
- `metrics series` takes N spaced snapshots (or reads them back with
  `--from-dir`) and reports first/last/delta/slope per metric with
  `directional_growth_leads` — the algorithm is the proven one from the
  android-memory-leaks skill script, and equivalence tests pin the two to
  the same fixtures. The interpretation line is contract: a lead is never
  called a leak.
- `metrics list-presets` reports which heavy profilers this host can run;
  `doctor` gains a `metrics` capability block (optional profilers never
  fail `--strict`). Snapshot artifacts land under session `metrics/`
  (0600), raw meminfo text beside the JSON.
- Process identity helper: pid via `pidof -s` (Android) / `launchctl list`
  + `pgrep` (iOS Simulator); failures carry `sources_tried`. New stable
  error codes: `app_not_running`, `tool_missing`, `preset_unavailable`,
  `trace_failed`.

## [0.27.0] - 2026-08-15

### Added
- Live session observation (research Phase 4 §2L). `session outputs`
  catalogs every followable session file — registered `streams[]` plus a
  conventional scan of `output/`, `logs/`, `network/` — with `abs_path` and
  a copy-pasteable `shell_hint` (`tail -f …`) for a human's second terminal.
- `logs follow` streams a session file (`--path`, `--source output:<name>`,
  or a stream id) or the device log (`--source device`) as NDJSON lines,
  always bounded by `--max-seconds` / `--max-lines`, with `--grep`,
  `--from-start`, and rotation-aware tailing. Files are confined to the
  session artifacts dir (`path_forbidden`).
- `network requests follow` polls the flow store and emits only new flows
  as NDJSON; `network requests list --since-id` pages from a cursor (an
  unknown cursor returns everything plus a `since_id_not_found` warning,
  never a silent empty). `journal --follow` tails the session timeline.
- Session records register their long-lived writers: the iOS `--log-stream`
  file and the mitm flow store appear in `session.json` `streams[]`; older
  sessions fall back to the directory scan.
- New stable error codes: `stream_not_found`, `path_forbidden`. Every
  follow ends with one `{"kind": "eof", "reason": …}` line — the NDJSON
  streaming exception is documented in COMPATIBILITY.md alongside
  `flow run --events`.

## [0.26.0] - 2026-08-15

### Added
- Local PR Proof: `autonom proof --base <ref>` reads the git diff, selects
  the smallest sufficient flow suite deterministically (changed flow files,
  `properties.covers` globs, `pull-request` tags), runs it against the
  active session, and writes `proof.json` + a one-screen `proof.md`.
  Verdicts are fixed and never upgraded: pass / fail / not_covered
  (uncovered changed files listed by name; exit 1) / blocked /
  inconclusive (exit 2).


## [0.25.0] - 2026-08-15

### Added
- Atlas-lite: a local observed-only application graph. Screen fingerprints
  are computed from snapshots the executor already holds (free) and ride in
  run events; `atlas update` folds runs and manual tap details into
  `~/.autonom/apps/<app-id>/atlas/graph.json` — screens keyed by a
  volatility-resistant structure hash (clocks, counters, list length, and
  the status bar do not move identity; real state changes become variants),
  transitions labeled by the triggering command with evidence references.
  `atlas show|coverage|paths|export|diff` query it; coverage explicitly
  reports the unknown as unknown.


## [0.24.0] - 2026-08-15

### Added
- Evidence bundle per flow run: `flows/<run_id>/manifest.json`
  (schema-versioned status, step records, artifact inventory, reproduction
  command), failure log windows beside the screenshots and hierarchy dumps,
  and run-scoped screenshot grouping under `shots/<run_id>/`.
- `autonom report build|open|export` — a fully self-contained HTML report
  (inline data: screenshots, restrictive CSP, everything escaped — UI text
  is hostile input) and JUnit XML for CI, rendered from the manifest alone
  so a report can be rebuilt from the same run forever.


## [0.23.0] - 2026-08-15

The research doc's first flagship workflow: Session → Flow.

### Added
- Instrumented `ui tap|type|find`: each action writes an owner-only detail
  record (proven selector, matched node, surrounding tree, typed text or —
  with the new `ui type --sensitive` — only its length) under
  `<session>/actions/`, linked from the journal via the `detail` key.
- `flow create --from-session <id|current>` compiles the journal + details
  into a validated canonical flow: proven selectors verbatim, explicit
  `--index` carried as explicit `index`, sensitive input as `${SECRET_n}`
  (never stored, credential-shaped fields auto-detected), the final
  verifying `ui find` as the closing assertion, coordinate taps and
  point swipes reported as warnings instead of approximated; the response
  includes a quality report and the exact replay command.
- End-to-end proof in tests: a fake-driver session records, compiles,
  passes `flow check`, and replays green with `--secret` — with the secret
  absent from every artifact.


## [0.22.0] - 2026-08-15

### Added
- Maestro Core Profile import/export (`flow import`, `flow export --format
  maestro`). Regex-by-default matching converts honestly (metacharacter-free
  patterns become `match: exact`; real patterns anchor as `^(?:...)$` regex);
  everything outside the profile — scripts, JS interpolation, point
  coordinates, random input — refuses with a positioned
  `unsupported_flow_command`, and an ambiguous conversion never produces a
  file that silently means something else. Imports validate end-to-end
  before they are written.


## [0.21.0] - 2026-08-15

Flow v1 surface completion; ten review-confirmed executor/language defects
fixed (see the commit "Fix ten defects the adversarial review...").

### Added
- Relational selectors: `above`, `below`, `leftOf`, `rightOf` (pure edge
  geometry against a provably unique anchor), `childOf` (ancestors),
  `containsChild` (direct children) — powered by an additive `parent` ref
  every compact-node snapshot now carries.
- `focused` selector/state field on both platforms; iOS `AXFocused` was
  misfiled under `focusable` and is now mapped correctly (iOS `focusable`
  reads false — the platform has no such concept).
- Commands: `longPressOn` (Android zero-distance swipe / idb `--duration`),
  `doubleTapOn`, `setOrientation` (Android `user_rotation`; refuses on iOS;
  invalidates the executor's cached screen size), `retry:` (explicit,
  max 3 attempts, `onlyOn` code filter, mutations demand
  `allowMutations: true`, no nesting/runFlow inside, every attempt in the
  journal and events), `group:` (labeled boundary events).
- CLI: `ui tap --duration MS` long-presses via the same adapters.


## [0.20.2] - 2026-08-15

Flow DSL v1, slice 3 of 3: composition, suites, packaging.

### Added
- `runFlow` execution: children run inline with the root `appId` inherited
  and their own env frame (child header env < runFlow env < `--env` <
  secrets); child hooks do not run; the graph was already statically
  contained and cycle-checked.
- Hooks: `onFlowStart` aborts the run when it fails; `onFlowComplete` runs
  after pass and fail with each command isolated — failures are reported as
  `hook_failures` and never mask the primary outcome; failure evidence is
  captured before cleanup.
- `when:` conditions on runFlow (platform / visible / notVisible /
  envEquals, AND semantics); a false condition skips with the reason.
- Tag-filtered directory suites: `flow run <dir> --include-tag --exclude-tag`.
- Remaining commands: `scrollUntilVisible` (bounded, single-fire swipes),
  `assertEnabled`, `assertChecked`, `setLocation`, `setPermissions`,
  `addMedia`.
- Evidence policy honored: `mode: always` captures per step,
  `beforeMutation`/`afterAssertion` in custom mode, `minimal` disables
  automatic captures; unsupported collect kinds warn.
- The CI emulator smoke now ends with a real
  `flow run tests/fixtures/flows/settings_smoke.yaml`.
- New `mobile-flow` skill (the 24th) + routing/docs sweep.

## [0.20.1] - 2026-08-15

Flow DSL v1, slice 2 of 3: the executor.

### Added
- `autonom flow run <file>` — executes one flow against the active session:
  pre-flight against the resolved target before any mutation, polling
  assertions (`time.monotonic`, injectable clock, default 10 s / 500 ms),
  single-fire mutations (a tap polls only while zero nodes match, then the
  ambiguity-refusing selection applies once), `--env`/`--secret`
  (values never enter artifacts), `--events` NDJSON streaming, `--dry-run`.
- Failure taxonomy: exit `1` + `failure_class: test_failure` on stdout for
  assertion timeouts and selector misses; exit `2` with an additive
  `failure_class` field for definition/infrastructure errors.
- Per-run `flows/<run_id>/events.ndjson` (versioned envelope, chmod 600),
  slim `flow_step` journal lines, failure evidence (screenshot + hierarchy).
- `role` selector field in the shared engine and `--role` on `ui find|tap`
  (additive; the 0.4.0-compat path is regression-tested).
- `ui.tap`/`ui.swipe` accept a cached `screen=` size, removing the extra
  accessibility dump per action on iOS.
- Contract probes `flow_check`, `flow_run_pass`, `flow_run_test_failure`
  (golden extended by hand; the probe harness now reads stdout for exit-1
  reports).

### Fixed
- The journal choke point recorded handlers that returned nonzero as
  `ok: true`; it now journals the real outcome (`doctor --strict`,
  `flow fmt --check`, and `flow run` test failures were all affected).

## [0.20.0] - 2026-08-15

Flow DSL v1, slice 1 of 3: the language and its static tools
(`docs/plans/PHASE_5_FLOW_DSL.md`; versions 0.16–0.19 stay reserved for the
metrics phase).

### Added
- `scripts/autonom_lib/flow/` — a stdlib-only strict YAML-subset flow
  language: positioned parser (every rejected construct carries
  `file`/`line`/`column`/`reason`), typed command registry with mutating
  flags and failure classes, `runFlow` graph validation with
  symlink-resolved workspace containment and cycle refusal, and a
  deterministic canonical emitter.
- CLI verbs `flow check`, `flow fmt [--write|--check|--diff]`, `flow list`.
- `docs/FLOW.md` — the language reference, with its surface machine-checked
  against the registry by `tests/test_docs_flow_surface.py`.
- New `error_code` family `flow_*` (network capture's `flow_not_found` is
  untouched and reserved to it).
- Test corpus and suites: parser accept/reject table, canonical round-trip
  and idempotence properties, schema and validator rules, bare-host entries.

## [0.15.2] - 2026-08-15

Reliability and release-engineering foundation. No CLI verbs or flags changed.

### Added
- GitHub Actions: `checks` (full `run_checks.sh` on ubuntu + macOS with pinned
  Python/Node, mandatory shellcheck, actionlint), `android-smoke` (real API-30
  emulator driving the Settings app through the CLI on pushes to main), and
  `release` (tag-gated tarball + `SHA256SUMS` upload).
- `tests/env_isolation.py` — shared save-and-restore environment sandbox for
  tests, plus first/last alphabetical guard modules that fail the suite when
  any test mutates `os.environ` without restoring it.
- `docs/COMPATIBILITY.md` — the written CLI compatibility policy that the
  contract golden, docs-surface gate, and error-code rules already enforce.
- This changelog.

### Fixed
- Test isolation: `tests/test_devices_lifecycle.py` and two sites in
  `tests/test_network.py` deleted ambient `AUTONOM_HOME` (and, in one case,
  `CI`) from the process environment instead of restoring them; six more test
  call sites reached the operator's real `~/.autonom` when `AUTONOM_HOME` was
  unset. The suite now leaves the environment and the real machine store
  untouched, in any test order.
- `scripts/build_release.sh`: version is read via the library (the same
  resolver `validate_plugin.py` trusts) instead of a brittle grep; required
  bundle files are pre-flighted loudly instead of silently skipped; checks run
  before staging; `dist/SHA256SUMS` is emitted.
- `scripts/run_checks.sh` redirects `AUTONOM_HOME` to a scratch directory for
  the whole run, prunes `dist/` from shell sweeps, and can require shellcheck
  (`AUTONOM_REQUIRE_SHELLCHECK=1`, set by CI).

## [0.15.1] - 2026-08-07

Initial public baseline (commit `c63e32e`): session/device lifecycle, UI
tree/find/tap, screenshots with embedded provenance, logs and crash access,
consent-gated network interception with redaction and HAR export, process
registry, journal, and 23 portable agent skills.
