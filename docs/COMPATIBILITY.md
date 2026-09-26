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
- Codes added in the unreleased hardening pass, pinned by
  `tests/test_error_codes_additive.py` together with every older value:

  | Code | Emitted by |
  | --- | --- |
  | `session_already_active` | `session start` and `tour --run` while a session is live (it used to be replaced silently) |
  | `ios_hid_framework_missing` | iOS input when idb's companion cannot load SimulatorKit (Xcode 27 with an old companion) |
  | `output_not_writable` | an `--out`/`--har`/export path that cannot be written, checked before the device is touched |
  | `invalid_simulator_action` | a `simulator` control given an action or category it does not have |
  | `flow_source_changed` | `teach approve` / `app-skill promote` when the flow or a `runFlow` child changed since its runs or receipt |
  | `comments_would_be_lost` | `flow fmt --write` on a file whose comments the canonical output would drop |
  | `signal_unstable` | a warning `code`, not an error: the Android live status-bar pin cannot read the signal back |
  | `stale_ref`, `run_not_found`, `step_not_found` | reserved; no verb emits them yet |

  `selector_required` already existed; it now also answers `ui find` and
  `network mock add` without a target.
- Warning codes added in the same pass (a `warnings[].code`, never an
  `error_code`, so not in `errors.py`): `stale_pin_dropped` — a status-bar or
  animation record taken on another emulator (or an earlier boot) on the same
  serial was dropped unsent; `pin_not_device_bound` — the emulator's boot
  identity could not be read, so the pin is not re-asserted; `stale_target` —
  `session stop` cleared a session whose target no longer exists. The
  matching response keys `stale_pin_dropped`, `pin_record_dropped`, and
  `stale_target` are additive.
- **One emitted code changed:** an unknown `simulator` action used to fail
  with `flow_command_invalid` and now fails with `invalid_simulator_action`.
  `flow_command_invalid` itself is unchanged and still answers bad
  `--value` keys and values. An agent that matched the old code for unknown
  actions should match both.

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
- **`verified` means read back.** A payload says `verified: true` only when
  the state it set was read from the device afterwards and matched. Where
  nothing can be read back the key stays and answers `false`, and a
  `verification` key says why (`read_back`, `mismatch`, `unavailable`,
  `unsupported`, or a reason string). Consumers that treated `verified` as
  "the command ran" must look at `ok` instead; `verified: false` with
  `ok: true` is a success whose effect is unconfirmed.
- iOS `network attach` keeps answering `attached: "unknown"` and adds
  `attach_state: "manual"` next to it. `network status` keeps `attached` as
  a bool or `"unknown"` exactly as before.

## Exit codes and streams

- `0` success · `2` expected failure (`AutonomError` as one JSON object on
  **stderr**: `{"ok": false, "error_code", "error", "hint"}`) · `130`
  interrupt. Argument errors (unknown verb, misspelt flag, a value that
  looks like an option) are the same envelope with `error_code:
  usage_error` and the usage line in `hint` — never argparse prose. `doctor` exits `0` unless `--strict` (then `1` when unhealthy, with
  `ok: false` and `strict_failures`) —
  a diagnostic that fails is useless in a pipeline (`scripts/autonom.py`).
  `ui wait --settled` exits `1` when the screen never settled within
  `--timeout-ms`, like `flow run`'s test failures.
- stdout carries exactly **one pretty-printed JSON document** per invocation;
  human prose and consent prompts go to stderr. Documented exceptions are
  opt-in streaming modes — `flow run --events`, `logs follow`,
  `network requests follow`, `journal --follow` — which emit NDJSON (one JSON
  object per line, ending with a `{"kind": "eof"}` line) and are never used by
  the repository's own gates — and `tour --human`, which prints the Markdown
  account for a person instead of the envelope.

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
