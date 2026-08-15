# Changelog

All notable changes to Autonom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as enforced by `scripts/validate_plugin.py` (the library version in
`scripts/autonom_lib/__init__.py` is the single source of truth).

## [Unreleased]

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
