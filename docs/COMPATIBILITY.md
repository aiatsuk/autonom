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
- The live-testing fix round (unreleased, after the hardening pass) adds one
  code, pinned by `tests/test_fix_ui_core.py` (`ErrorCodeTests`):

  | Code | Emitted by |
  | --- | --- |
  | `element_offscreen` | `ui tap` (and a long press) on a node that is not laid out on screen — iOS lists Flutter's scroll cache at a 0x0 frame, and the tap used to land on the screen's origin and report success. The envelope carries `ref`, `bounds` and `match_count`; nothing is dispatched. A flow classifies it as `test_failure` |

- **Emitted codes that changed in the same round** — each used to be the
  wrong answer, and each is pinned by a regression test:

  | Situation | Was | Now |
  | --- | --- | --- |
  | `simulator push send` without `app_id` (and no session app) or without a JSON `payload` | `flow_command_invalid` | `invalid_value`, with `missing` |
  | Android `simulator clipboard set` on a build without `cmd clipboard` (API 36) | exit 0, `ok: true`, `supported: false` and a `clipboard_unsupported` warning | exit 2, `unsupported_capability` (`reason: clipboard_unsupported`, `supported: false`) |
  | Android `simulator clipboard get` | `invalid_simulator_action` (there was no `get`) | `unsupported_on_platform` — `get` exists now and reads the iOS pasteboard |
  | iOS `simulator clipboard get` with no usable `xcrun` | `backend_failed` ("could not read the pasteboard") | `simctl_not_found`, like every other control |
  | Android `logs tail --package X` / `logs follow --source device --package X` where X is not installed | exit 0 with unfiltered (or no) lines | `app_not_installed` |
  | `ui tap` on a node with no area | exit 0, a tap at (0, 0) | `element_offscreen` |
  | `ui tap` / `ui find` whose selector matched one node on screen and an off-screen copy (iOS) | `ambiguous_selector` | the on-screen node; `--index` counts on-screen matches, so an index past them is `selector_index_out_of_range` (every match is counted only when none is on screen) |
  | `flow run <dir>` with a flow whose platforms exclude the target (`requires.platform` names the other platform, or an Android-only step on iOS — `flow_validator.flow_platforms`) | the suite aborted with that flow's pre-flight code (`unsupported_on_platform`, `unsupported_key_for_platform`, or `flow_requirements_unmet` for the declared platform) | the flow is skipped with a `flow_skipped_for_platform` warning and listed in `skipped_flows`; when no flow can run, `flow_no_flows_found` (with `skipped`). Only a platform mismatch is skipped: a flow refused for anything else — a missing capability from `requires.capabilities` or `setup` (`flow_requirements_unmet` from the provider pre-flight) — still stops the suite as before, and a single file is still refused at its step |
  | Android `network attach` when adb cannot read the current global proxy | exit 0; adb's error text was saved as the previous proxy and written back to the device by `detach` | `backend_failed` before anything is written |

- Warning codes added in the same round (`warnings[].code`, never an
  `error_code`): `url_opened_chooser`, `url_not_handled` (`open`);
  `unverified_skipped` (a recorded pid that `ps` cannot vouch for was not
  signalled: `cleanup`, `session stop`, `record stop`, and the log-stream
  restart after `simulator keyboard pin` / `session clear --strategy
  reinstall`);
  `log_filter_degraded`, `process_name_not_filtered` (Android `logs tail` /
  `logs follow --package`); `log_stream_stopped` (`simulator keyboard pin`
  with a reboot, `session clear --strategy reinstall`);
  `companion_left_running` and `group_remnant_left_running` (`session stop`;
  the latter also `cleanup` and `doctor`);
  `flow_skipped_for_platform` (`flow run <dir>`); `no_frames` (`metrics frames
  capture`); `cpu_stale` (`metrics snapshot`); `flutter_proxy_hook_required`
  (iOS `network attach`); `screen_not_settled` (flow `waitForSettled`);
  `launch_args_not_compilable`, `clear_state_not_compilable_on_ios`,
  `uninstall_not_compilable`, `open_url_not_compilable`,
  `open_url_not_recoverable` (`flow create`).
  `network status` now raises `persistent_mocks_active` as `network start`
  and `doctor` do.

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
  `attach_state: "manual"` next to it; its `mode` changed from `"automated"`
  to `"manual"`, so the two no longer contradict each other (`mechanism`
  names what was automated). `network status` keeps `attached` as a bool or
  `"unknown"` exactly as before.

### Behaviour and value changes in the live-testing fix round

Key sets stay additive except where a row says a key is dropped; these
change what a value means or what a verb does, so an agent that branched on
the old answer must read them.

| Verb | Before | Now |
| --- | --- | --- |
| `network status` | any flow in the last minute made `attached: true, evidence: recent_flows` — a host `curl` through the proxy included | a flow counts only when its client is the target: Android guest flows (`10.0.2.x`) give `target_flows`; loopback flows are unattributed (the emulator's NAT and a host process look the same) and the device's proxy setting, read back, decides (`device_setting`, `device_proxy_cleared_externally`, or `setting_unreadable` when adb cannot read it); on iOS traffic never proves attachment (`"unknown"`, `host_traffic_indistinguishable`, with a `reason`). `recent_flows` is never answered; `target_flow_count`, `unattributed_flow_count`, `recent_user_agents` are additive |
| `ui find --all` | matches in tree order | on-screen matches first (tree order), then off-screen ones — position *i* is what `--index i` selects; each match carries `index` (null for an off-screen match no index reaches while another is on screen). With no match on screen the order is the tree's, as before |
| `ui find` (no `--all`), `ui tap` | every match counted, off-screen copies included | on-screen matches first (see the code table above) — the node `ui tap` acts on is the node `ui find` answers; its match carries the additive `index` too (the `--index` that selects it) |
| `ui wait --settled`, flow `waitForSettled` | an unsettled result always read "the tree was still changing … (0 change(s) over 1 snapshots)" | "still changing" only when the last snapshot was a change; "the tree changed N time(s), then held still for X ms, less than the Y ms quiet window" when it settled too late; "could not confirm the screen settled: only 1 snapshot fit in N ms (a dump takes ~M ms); raise --timeout-ms" (or identical snapshots that held still for less than the quiet window) when nothing was seen changing — with one hint per case. Same code (`screen_not_settled`), same exit 1 / `ok: false`; the result gains `dump_ms` (the slowest dump) and `stable_ms` (how long the last tree held still) |
| `session stop`, `record stop`, `record start` | signalled (or, for `record start`, trusted) the log-stream and recorder pids stored in the session file | a pid is signalled only once its command line shows it is still that process — whole arguments, never substrings: the bounded writer's own script line plus the session's stream file followed by its cap (not `tail -f <file>`, not `<file>.bak`); `recordVideo`, the UDID and the recording path; `screenrecord`, `/sdcard/autonom-recording.mp4` and the adjacent pair `-s <serial>` (a session on `R58M123` never claims `R58M123ABCD`'s recorder) — and, where there is a registry row, its signature too; otherwise nothing is sent (`unverified_skipped` warning when `ps` cannot say) and `session stop` adds `teardown_verification`. `teardown[].detail` keeps its bool type |
| log-stream restart (`simulator keyboard pin` reboot, `session clear --strategy reinstall`) | — (new in this round) | a reboot never signals the old writer; a reinstall stops it only once verified. A recorded pid that now runs something else is not the writer, so a new stream is started; one `ps` cannot vouch for is left alone with no second writer (`unverified_skipped`) |
| `metrics frames capture` | a window of 0 frames reported gfxinfo's `percentile_*_ms` (its histogram's top bucket, 4950 ms) and `janky_percent: 0` | those keys are **dropped** for 0 frames, and `no_frames` is warned (also at the top level) |
| `metrics snapshot` (Android) | `cpu.process_percent` from `dumpsys cpuinfo`, whose window may have closed minutes earlier | a fresh sample (two `/proc` reads 0.5 s apart) on the one-core scale (two busy cores read 200); cpuinfo only as the fallback, with its window in the additive `cpu_sampling` and a `cpu_stale` warning when old |
| `metrics trace --preset simpleperf` | `simpleperf record -p <pid>` with the default event | `record --app <id> -e cpu-cycles` when `simpleperf list hw` lists it, else `cpu-clock` |
| `metrics list-presets` | `hitches` available wherever xctrace is | `hitches` is `available: false`, `reason: unsupported_on_simulator` (Instruments refuses it on the Simulator) |
| `session launch` (Android, resume) | `monkey` (which reset a pinned rotation) | `am start -W -n <launcher> -a MAIN -c LAUNCHER -f 0x10200000`, the launcher's own intent, with `component` and the `launch` report; `monkey` only when no launcher activity resolves. An explicit `--activity` resume now waits (`-W`, 60 s) |
| `processes` | `live`, `orphans`, `stale_entries`, `harness` | adds `background` (session-owned log streams, canvas pairs, companions) and `group_remnants`; `live` stays proxies only |
| `cleanup` (default) | killed any registered orphan | kills a registered non-proxy process only while it runs the recorded command and (its session stopped, or it is a supervised child whose supervisor died, or it has no owner and no intact artifacts directory). Never: harness rows (emulators), a running session-less supervisor, a child whose supervisor lives, a live session's rows, or a group whose leader is gone |
| `cleanup --all` | proxies | also the `background` category, under the same checks |
| `cleanup` results | `terminated`, `termination_failed` | actions may also be `already_exited`, `pid_reused`, `unverified_skipped`, `group_remnant` (no signal sent for the last three); adds `skipped_unverified`, `group_remnants`, `skipped_group_remnants`; `still_live` counts `background` too |
| `session stop` | stopped the recorded log-stream pid, recorder, proxy | also terminates every registry row the session owns (after a signature check) and records `process_teardown`; a companion it did not start and a group remnant are reported, never killed |
| process registry rows | detail keys stored as given | keys whose value is null are dropped; a `command` is stored redacted; a `signature` may be a list of marks that must all appear (an `idb_companion` row records the binary and its simulator's UDID; a log-stream row the writer's script line and ` <stream file> `) |
| `cleanup`, `session stop` on a row whose `signature` is empty (`""`, `[]`, `[""]`, null) | signalled without any check (an empty signature was skipped as if absent) | `unverified_skipped`, never signalled: an empty mark matches every command line. Only a row with no `signature` key keeps the unchecked stop — a proxy found by its command line in the same scan, and rows written before signatures existed (harness emulator rows have none too, but are never stopped here) |
| `logs follow` | a final unterminated fragment that reached `--max-lines` ended with eof reason `stream_ended` | `max_lines` |
| `logs tail` / `logs follow` (Android `--package`) | `--pid` of a running process, else every line | the app's uid on API 31+ (and the lifecycle lines naming it; earlier processes of the app included), the running pid on older APIs (`log_filter_degraded`), else lines naming it; `filter` is reported; `com.x:remote` filters by `com.x`'s uid with `process_name_not_filtered` |
| `logs tail` / `logs follow` / `--log-stream` (iOS `--package`) | a predicate on the bundle id's last component, which never names a Flutter app's `Runner` | the installed bundle path (`processImagePath BEGINSWITH`), else the executable, else the subsystem; the `log` tool's banner and trailer are dropped; `executable` is reported |
| `flow check`, `flow list` | `platforms` was `requires.platform` or both | inferred: both minus iOS when a step that runs there is Android-only |
| `report suite --last N` | the N most recent runs | the latest run of each of the N most recently run flows (a re-run replaces its flow's earlier run); `report history --last` still counts runs |
| `flow export --format maestro` | a tap with `timeoutMs` refused | exported as `extendedWaitUntil: {visible: <selector>, timeout: N}` then the tap |
| `session outputs` | session streams and the journal | also `metrics/` and `recordings/` entries (additive `followable`, `directory`); the open hint is `open` on macOS, `xdg-open` where installed, absent otherwise |

`waitForSettled` is a new Flow command (additive), declared `since` 0.31.0 —
the release that will carry it; the version is not bumped until then.

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
