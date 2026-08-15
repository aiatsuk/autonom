# CLI compatibility policy

Autonom's CLI is consumed by agents that branch on machine-readable output.
This document is the written half of a policy the repository already enforces
mechanically; every rule below names its enforcement point. Rules may be
strengthened, never silently weakened.

## Error codes

- `error_code` values may be **added but never repurposed or removed** —
  host agents branch on them (`scripts/autonom_lib/errors.py`, module
  docstring).
- The code `flow_not_found` belongs to **network capture** (a recorded HTTP
  request is a "flow" in mitmproxy's vocabulary; `scripts/autonom_lib/network/store.py`).
  It is reserved there forever. The Flow DSL (house Phase 5) uses a distinct
  code family (`flow_file_not_found`, `flow_parse_error`, …) and must never
  mint `flow_not_found`.

## Response shape

- Response keys are **additive-only**. Removing, retyping, or renaming a key
  fails `tests/contract_probe.py` `compare()` against
  `tests/fixtures/android_contract_golden.json`.
- The golden is **never regenerated in a feature PR**: `contract_probe.py
  --write` re-blesses whatever the current build emits, silently erasing the
  protection. New probes are appended to the fixture by hand.
- `--serial` and the `serial` response key are **permanent** (DEC-004,
  `scripts/autonom_lib/platform.py`; guarded by
  `tests/test_contract_golden.py`). Device-touching payloads always merge
  `target.identity()` — `platform`, `target_id`, and `serial` on Android.
- Success payloads carry `"ok": true`. Soft problems ride in a `warnings`
  array whose entries use the key `code`; hard failures use `error_code`.
  This asymmetry is frozen as-is — smoothing it over would break both kinds
  of consumer.

## Exit codes and streams

- `0` success · `2` expected failure (`AutonomError` as one JSON object on
  **stderr**: `{"ok": false, "error_code", "error", "hint"}`) · `130`
  interrupt. `doctor` exits `0` unless `--strict` (then `1` when unhealthy) —
  a diagnostic that fails is useless in a pipeline (`scripts/autonom.py`).
- stdout carries exactly **one pretty-printed JSON document** per invocation;
  human prose and consent prompts go to stderr. Documented exceptions are
  opt-in streaming modes — `flow run --events`, `logs follow`,
  `network requests follow`, `journal --follow` — which emit NDJSON (one JSON
  object per line, ending with a `{"kind": "eof"}` line) and are never used by
  the repository's own gates.

## Documentation gates

- Every CLI verb and long flag must appear in `docs/CAPABILITIES.md`
  `## CLI surface`, in both directions
  (`tests/test_docs_cli_surface.py`).
- Every verb must fail on a bare host with one machine-readable `error_code`
  and no traceback (`tests/test_bare_host.py`).

## Versioning and releases

- Single version source: `scripts/autonom_lib/__init__.py` `__version__`.
  The two plugin manifests must match it (`scripts/validate_plugin.py`), and
  the release workflow refuses a `v*` tag that disagrees
  (`.github/workflows/release.yml`).
- Releases are built by `scripts/build_release.sh` (tarball + `SHA256SUMS`)
  and published from tags; `CHANGELOG.md` carries the notes.

## Forward commitments

- The Flow DSL will add a `failure_class` field
  (`test_failure` | `flow_definition` | `infrastructure`) to flow-verb
  results and error envelopes, plus exit code `1` for *test* failures of
  `flow run` (following the `doctor --strict` precedent). Both changes are
  **additive** — existing envelope keys, codes, and exit semantics are
  unchanged.
