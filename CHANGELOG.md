# Changelog

All notable changes to Autonom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as enforced by `scripts/validate_plugin.py` (the library version in
`scripts/autonom_lib/__init__.py` is the single source of truth).

## [Unreleased]

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
