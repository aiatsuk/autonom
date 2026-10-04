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
- Codes added in the 0.31.0 hardening pass, pinned by
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
- The live-testing fix round (0.31.0, after the hardening pass) adds one
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
- **Transparent Android capture is additive.** `network attach --system-ca`
  answers `mode: "transparent"` with `capture_mode: "transparent"`,
  `http_proxy_routed`, and `system_ca` (`{installed, hash, api, method,
  reversible, verified, checked_via}`); the app-proxy path now also carries
  `capture_mode: "app_proxy"` in the session network record. `network status`
  gains `capture_mode` (and, on the transparent path, `system_ca` and
  `http_proxy_routed`). `devices boot --http-proxy HOST:PORT` adds `http_proxy`
  to the boot payload. No existing key changed type or meaning. The refusals
  reuse the existing `unsupported_capability` code (never a new code) with a
  `capability` extra: `network.system_ca` on a non-rootable / Play image
  (`reason: adb_root_refused|adb_root_unavailable|physical_device`), and
  `network.transparent_capture` when the emulator was not booted proxy-routed
  (`reason: emulator_not_proxy_routed`, with `expected`/`observed`) or while an
  app-proxy attach is active (`reason: app_proxy_attached`, with
  `device_proxy`; `network detach` restores the device's saved global proxy
  first — switching modes on top of it would lose that value).
- **`network status` in transparent mode** answers `attached: true` with a new
  additive `evidence` value `"transparent_proxy"`, `target_flow_count ==
  recent_flow_count`, and `unattributed_flow_count: 0`, while the process
  registry still shows the session's emulator booted routed through the
  recorded proxy (a registry read, never a device call). Every flow on the
  proxy is then *counted* as the device's regardless of its `client_ip` (all
  `127.0.0.1` by design); a host process using the port would be counted too,
  which is the accepted default on a dedicated test host. The loopback
  attribution heuristic (`scripts/autonom_lib/network/attachment.py`) is
  skipped only in that case. Once the route is gone (`devices shutdown`, or a
  reboot without `--http-proxy`) status answers `attached: false` with the
  added `evidence` value `"transparent_route_gone"` and a `reason` (unless a
  guest-network `10.0.2.x` flow still proves attachment, as on the app-proxy
  path); loopback
  flows are unattributed again, and the device proxy setting is not read
  (transparent mode never wrote one). `network requests` never filtered by
  client attribution, so it lists these flows either way; `attachment.py` value
  types are unchanged (`transparent_proxy` and `transparent_route_gone` are
  added `evidence` strings).
- **`system_ca_unverified`** is an added `warnings[].code` (never an
  `error_code`, so not in `errors.py`): `network attach --system-ca` installed
  the CA but could not confirm it from a zygote mount namespace. The attach
  still stands (`attached: true`) with `system_ca.verified: false` — HTTP is
  captured and the change is reversible.
- **Verification scope.** The transparent path is verified live only on
  **API >= 34** (APEX conscrypt, `method: apex_conscrypt`). The API < 34 path
  (`method: system_cacerts`, the `/system/etc/security/cacerts` remount) is
  implemented but **not yet verified on a device**; it is documented as such and
  not claimed to work.
- **A recorded flow's `host` is the requested name.** The addon
  (`scripts/autonom_lib/network/mitm_addon.py`) used to record
  `flow.request.host`, which is the CONNECT target; under the emulator's
  `-http-proxy` every CONNECT names an IP, so `host` read `130.193.59.68` while
  the app asked for `backend.woolbox.app`, and `network requests list --host` and
  `network mock add --host` both missed every flow. `host` (and host-based mock
  matching) now use mitmproxy's `pretty_host` — the Host header / `:authority`,
  falling back to `request.host`. When the CONNECT target is a literal IP that
  differs from the name, it is kept in an additive `server_ip` field. The key
  set and type are unchanged; the value differs only where the Host header and
  the CONNECT target disagree (in practice, the transparent path). Flows recorded
  earlier keep their stored `host`.

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
| process registry rows | detail keys stored as given | keys whose value is null are dropped; a `command` is stored redacted; a `signature` may be a list of marks that must all appear (an `idb_companion` row records the binary and its simulator's UDID; a log-stream row the writer's script line and ` <stream file> `). A log-stream row whose signature is a bare stream path (written before that) matches only the bounded writer of exactly that file, never `tail -f <file>`. `ps` is read under a UTF-8 locale whatever the caller's, so a path under a non-ASCII `AUTONOM_HOME` is compared as itself; `session stop` keeps a log-stream row whose own signature still matches the live process, and one `ps` could not render (`unverified_skipped`) |
| `cleanup`, `session stop` on a row whose `signature` is empty (`""`, `[]`, `[""]`, null) | signalled without any check (an empty signature was skipped as if absent) | `unverified_skipped`, never signalled: an empty mark matches every command line. Only a row with no `signature` key keeps the unchecked stop — a proxy found by its command line in the same scan, and rows written before signatures existed (harness emulator rows have none too, but are never stopped here) |
| `logs follow` | a final unterminated fragment that reached `--max-lines` ended with eof reason `stream_ended` | `max_lines` |
| `logs tail` / `logs follow` (Android `--package`) | `--pid` of a running process, else every line | the app's uid on API 31+ (and the lifecycle lines naming it; earlier processes of the app included), the running pid on older APIs (`log_filter_degraded`), else lines naming it; `filter` is reported; `com.x:remote` filters by `com.x`'s uid with `process_name_not_filtered` |
| `logs tail` / `logs follow` / `--log-stream` (iOS `--package`) | a predicate on the bundle id's last component, which never names a Flutter app's `Runner` | The `log` predicate (`log stream`, `log show`) is the subsystem or the executable (`processImagePath`/`senderImagePath ENDSWITH "/Runner"`), else a distinctive last component — never the container path: after a reinstall of the same build the unified log keeps reporting the first container the binary was seen at. It admits every app with that executable name (every Flutter app is a `Runner`), so lines are narrowed on the client by image UUID: a record whose `processImageUUID` or `senderImageUUID` is one of the installed binary's LC_UUIDs (or one the session recorded) is kept whatever its path, and one whose UUIDs are all other is dropped even under the app's path; with no UUID known, the installed bundle path, else the executable, decides as before. `--package` narrows `logs tail` (the session's stream file, else `log show`, whose output it used to take as the predicate gave it) and `logs follow --source device` — live, from the current session's file (plus the UUIDs that session recorded) and in a `--session-id` replay — to that app; `logs follow --path`/`--source <stream id>` take no package (as before, `--package` is ignored there). The replay matches the session's own app (`--package` equal to its `app_id`) by the identity its stream recorded — `log_stream_executable`, `log_stream_image_uuids`, without asking the device — and any other `--package` by its installed binary, resolved fresh (it used to apply the recorded executable to whatever package was asked for). The session's `--log-stream` file holds the raw records of every same-named app, and they count against the writer's cap (`AUTONOM_IOS_LOG_MAX_MB`, one rotation): a chatty other `Runner` rotates the app's own records out sooner. They are not dropped when written, deliberately: a build installed while the stream runs (outside Autonom) has UUIDs the writer does not know, and a write-time filter would lose its records for good; read against the installed binary, they are kept. So a read of the session's own stream file (`logs/stream.ndjson` or its `.1` rotation — the same file, so a hard link or a case variant on a case-insensitive volume counts too) without `--package` is narrowed to the session's app: `logs follow` by default, with `--source log_stream` or `--path logs/stream.ndjson[.1]`, `logs follow --source device` from the session's file or in a `--session-id` replay, and `logs tail`. The recorded identity is used at once — `log_stream_executable`, `log_stream_image_uuids`, and the binary at `log_stream_bundle_path` read again from disk while that install is still there — with no device asked. That install counts only while its `Info.plist` names the session's `app_id`: a recorded path now holding another app's bundle is treated as gone, and a bundle whose `Info.plist` names another identifier never lends its binary's UUIDs (a missing or unreadable plist, or one naming no identifier, still falls back to the executable name when the binary is looked up). Only for the current session, and only when that install is gone (a reinstall, perhaps of another build), belongs to another app or was never recorded, is the simulator asked for the installed app, for at most 5 s, and a probe that fails or times out keeps the recorded identity without an error. A package-less `--session-id` replay never asks the device (nor one whose `--package` is the session's app; another `--package` is resolved from its installed binary, as above); the file is read raw when the session has no app or nothing identifies it — a past session that recorded no identity, a current one that recorded none and whose app is not installed. Any other `--path` is followed raw, as before; a live `logs follow --source device` without `--package` is the whole device log, and so is `logs tail` from `log show` without one. `session outputs` lists the file with a `follow_hint` (`logs follow --path logs/stream.ndjson`, narrowed as above) and a `shell_hint` (`tail -f`), which shows the raw file. Flow evidence (`--collect logs`, failure evidence) and report bundles never read the stream file: the evidence is `log show`, narrowed to the session's app when it has one. The session record gains the additive `background.log_stream_image_uuids` and `background.log_stream_bundle_path` — what the writer was started with, taken from the lookup that named its predicate; `logs tail` and the follow's `eof` line gain the additive `image_uuids` (sorted) when records were matched by UUID, and `logs tail` reports `executable` for a package-less read of the stream too. The `log` tool's banner and trailer are dropped. |
| `flow check`, `flow list` | `platforms` was `requires.platform` or both | inferred: both minus iOS when a step that runs there is Android-only |
| `report suite --last N` | the N most recent runs | the latest run of each of the N most recently run flows (a re-run replaces its flow's earlier run); `report history --last` still counts runs |
| `flow export --format maestro` | a tap with `timeoutMs` refused | exported as `extendedWaitUntil: {visible: <selector>, timeout: N}` then the tap |
| `session outputs` | session streams and the journal | also `metrics/` and `recordings/` entries (additive `followable`, `directory`); the open hint is `open` on macOS, `xdg-open` where installed, absent otherwise |

`waitForSettled` is a new Flow command (additive), declared `since` 0.31.0,
the release that carries it.

### Mobile Canvas scrcpy transport (0.31.0)

No `error_code` is added. `canvas serve` refuses with existing codes, checked
before node starts:

| Situation | Code |
| --- | --- |
| `--scrcpy-version` that does not look like `4.1`, `--scrcpy-version` without `--scrcpy-server`, a `--scrcpy-server` that is not a file, `--max-size` outside 320..4096, `--bit-rate` outside 100000..100000000 | `invalid_value` |
| `--transport scrcpy` or `--scrcpy-server` for an iOS Simulator | `unsupported_on_platform`, `capability: "canvas.scrcpy"` |
| `--transport scrcpy` when no source is configured at all (no flag, no `AUTONOM_SCRCPY_SERVER`, no `SCRCPY_SERVER_PATH`, no `scrcpy` on `PATH`) | `tool_missing`, `tool: "scrcpy"`, `capability: "canvas.scrcpy"`, with the install hint |

A configured server that is missing or is not 4.1 is found by the Canvas
itself: with `--transport scrcpy` it exits non-zero with one stderr line
that starts `android-emulator-browser: canvas.scrcpy:` and carries the
install hint; with `auto` it falls back and says why. The Canvas speaks the
scrcpy 4.1 protocol only (scrcpy keeps no compatibility between releases),
so upgrading scrcpy turns the transport off until Autonom supports the new
version; nothing else breaks.

| Surface | Before | Now |
| --- | --- | --- |
| `canvas serve --transport auto` on Android | `screenrecord` + ffmpeg, else `screencap` | scrcpy when a scrcpy-server 4.1 resolves (`--scrcpy-server`, `AUTONOM_SCRCPY_SERVER`, `SCRCPY_SERVER_PATH`, an installed `scrcpy`), then `screenrecord` + ffmpeg, then `screencap`. On scrcpy the page streams over `/ws/video` and sends input over `/ws/control` instead of `/stream.mjpeg` and the HTTP input endpoints. `--transport screenrecord` or `screencap` keeps the earlier choice |
| `canvas serve --fps` | always forwarded, default 15 | forwarded only when given: the multipart default stays 15, and scrcpy caps at 60 unless `--fps` is given |
| `/status` | — | `transport` may be `scrcpy`; additive `fallback_reason` (null unless `auto` on Android skipped a transport) and `scrcpy` (null unless a 4.1 server resolved: `version`, `server_path`, `source`, `session_state`, `width`, `height`, `packets`, `bytes`, `key_frames`, `restarts`, `video_clients`, `control_clients`, `journal_pending`, `journal_dropped`) |
| Android H.264 detection (`screenrecord_h264`, `/stream.h264`, the `screenrecord` transport) | `--output-format` listed by `screenrecord --help` | a `screenrecord --output-format=h264 --time-limit 1` run of at most 5 s must yield an H.264 SPS. API 36 targets, whose help omits the hidden option although it works, now get `screenrecord_h264: true`; Canvas start can take up to 5 s longer on a device without H.264 output |
| Any Canvas request or upgrade whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` | served | 403 `Host not allowed`; a port forward must keep the Canvas port and use one of those names |
| A plain HTTP request that carries an `Upgrade` header but is not a WebSocket handshake for `/ws/video` or `/ws/control` (`Upgrade: h2c` from `curl --http2` or Java's HttpClient) | served | still served by the normal handler; on Node versions whose `http.createServer` ignores `shouldUpgradeCallback`, on a connection closed after the answer, and there a request with a body (any `Transfer-Encoding`, or a `Content-Length` other than zero) gets 400 asking to resend it without the `Upgrade` header, while a zero `Content-Length` (`0`, `00` or any run of zeros) is accepted as no body |
| The page loaded in a frame | shown | refused: 403 for framed loads (`Sec-Fetch-Dest` iframe, frame, embed, object, fencedframe), `frame-ancestors 'none'`, `X-Frame-Options: DENY` |
| `POST /auth` without a token | 401 | with a valid session cookie, from a same-origin fetch, `{"ok": true, "csrf": ...}` so a reloaded page resumes; otherwise 401, or 403 for another origin |
| Multipart clients (`/stream.mjpeg`) | one capture loop per page | one shared loop per Canvas; a client more than 2 MiB behind skips frames |
| `doctor` | — | additive `optional_tools.scrcpy` (`state`, `ready`, `optional`, `capability`, `path`, `server_path`, `version`, `required_version`, `source`, `install_hint`, `error`), kept out of `tools` so it never affects `--strict`; `overrides` may list `AUTONOM_SCRCPY_SERVER` and `SCRCPY_SERVER_PATH` |
| `scripts/bootstrap.sh` | arguments other than `--install` ignored | `--with-scrcpy` added (with `--install`, installs scrcpy); any other argument prints the usage and exits 2; an "Optional tools" section reports scrcpy without ever failing the check |
| Journal | — | the scrcpy path adds the verbs `ui gesture`, `ui scroll`, `ui paste`, `ui system` and `ui control`, and writes `ui key` and `ui text`, all with argv `ui <kind> <canvas>` and a `canvas-<kind>` action detail carrying `transport: "scrcpy"`. Its `key` is the numeric Android keycode where the HTTP path writes a `KEYCODE_*` name |

`/tap`, `/swipe`, `/key`, `/text`, `/control`, `/frame`, `/stream.mjpeg`,
`/stream.h264` and the iOS path keep their behaviour; `/control` now also
lifts the pointers and keys of WebSocket connections it refuses. `/text`
keeps its conservative ASCII rule; Unicode text exists only on the scrcpy
transport, where it replaces the device clipboard.

### Mobile Canvas display presets (unreleased)

Additive. No `error_code`, CLI verb or flag is added, and a Canvas that never
changes the display runs no `wm` command and behaves as before.

| Surface | Before | Now |
| --- | --- | --- |
| `POST /display` | 404 | `{"preset": "small" \| "pixel-11" \| "pixel-fold" \| "tablet" \| "default"}`, under the token or cookie plus `X-Autonom-CSRF`, `Host` and handoff rules of `/tap`. 200 `{"ok": true, "display": {"preset", "width", "height", "density"}}` once the change reads back; 400 for an unknown preset (naming the valid ids) or on iOS (`Display presets are Android-only`); 409 when paused, owned by another origin, or replaced by a newer request (`superseded: true`); 502 when a `wm` command fails or the read-back differs (naming the command and the values in effect); 503 while the Canvas stops. A refused request runs no `wm` command |
| `POST /tap`, `POST /swipe` | `x`, `y` (`x1`, `y1`, `x2`, `y2`, `duration`) in display pixels | additive optional `dw`, `dh`: the display size the points were computed for. When both are given and differ from the size read back when the input's turn comes, the points are scaled to that size, so a page that has not yet polled a display change made elsewhere still taps where it shows; the page sends them once it knows the size. 400 (`dw and dh must both be integers from 1 to 10000`) with no device action when only one is given or either is not such an integer. Requests without them, as agents send, behave as before |
| `/ws/control` | `{"t": "display"}` refused as an unknown message type | `{"t": "display", "preset": ...}` accepted under the handoff rules; replies `{"t": "display", "ok": true, "display": {...}}`, or `error` with `for: "display"` (and `superseded: true` for a replaced request) |
| `/status` `display` (Android) | `width`, `height` from the action bridge | `width`, `height` and the additive `density` from `wm size` / `wm density`, read at most every 2 s (the bridge's size when `wm` cannot be read); additive `preset` (a preset id, `default`, or null for an override the Canvas did not make) and `presets` (`id`, `label`, `width`, `height`, `density`; `default` has no numbers). iOS unchanged |
| `state` message | — | additive `presets`, and `preset` and `density` from a reading at most 2 s old |
| `window.autonomCanvas.stats()` | — | additive `display` (`preset`, `density`, `picker`, `pickerDisabled`) |
| Journal | — | adds the verb `ui display` (argv `ui display <canvas>`, a `canvas-display` action detail) with `preset` (a preset id, or `restore` for the stop-time restore by `system`), `width`, `height`, `density`, origin and `transport` (`scrcpy`, `screenrecord` or `screencap`). The bridge allowlist grows by this kind only |
| Canvas stdout | — | `Display: <preset> <W>x<H> @ <density>` per change; `Display restored: <W>x<H> @ <density>` or `Display restore failed: ...` at stop |
| Canvas stop (SIGINT, SIGTERM, SIGHUP) | ends the device server and its `adb forward` | also puts back the size and density overrides found before the first display change, within the 3 s shutdown budget; nothing when the Canvas made no change |
| The page | one side panel of text buttons | rebuilt: toolbar with the Android Size menu, device frame, a pill of icon buttons, an inspector, light and dark themes. Every control stays reachable, several under a new name: Apps is Recent apps, Vol − and Vol + are Volume down and Volume up, Wake is Wake screen, Alerts is Notifications, Quick is Quick settings, Collapse is Collapse panels, Clipboard is Copy device clipboard, Type text is Send, and the ↑ ↓ ← → text buttons are icon-only buttons named Up, Down, Left and Right. Back, Home, Recent apps, Rotate, Volume down, Volume up and Power (the pill) and Delete (Keys) are icon-only buttons named by `aria-label` and tooltip; Enter keeps its text; Reconnect stream moves under Stream in the inspector. Take control and Pause input buttons are new, and the ids `video`, `screen`, `status`, `text`, `clipboard`, `device`, `refresh`, `window.autonomCanvas` and its `stats()` / `send()` keep their meaning; the status text now sits in the collapsed Diagnostics panel |

A Canvas killed with SIGKILL leaves the preset on the device until
`adb -s <serial> shell wm size reset` and `wm density reset`; a second Canvas
on the same device takes the first one's preset as its original. Both are
documented in the `android-emulator-browser` skill.

### iOS UI recovery and `--session-id` (unreleased)

Additive, with two behaviour changes listed at the end. A host that passes
neither `--ui-backend` nor `--session-id`, on a screen whose idb tree is
usable, sees the same results plus additive keys.

| Surface | Before | Now |
| --- | --- | --- |
| `--ui-backend auto\|idb\|xcuitest` | — | new flag (before or after the verb), `AUTONOM_UI_BACKEND`, and `session start --ui-backend`, stored as the session's `ui_backend_preference`. `auto` is the default and keeps idb; `--ios-hid` still routes idb-side input (idb or AXe) |
| `--session-id ID` | `session outputs`, `journal` and `logs follow` only, to read a past session | every device verb, and the top-level parser (`autonom --session-id ID report build`). The command uses that session's record and artifacts, and an active session's journal, and never moves the current pointer. The three read-only uses keep working, also on stopped sessions, as do `shots`, `report` (except `report serve`, whose `/replay` drives the device: `session_stopped`) and `session show`; nothing rewrites a stopped session's `session.json`; a stopped session's journal never grows, and the command is journaled in the current session, if any, as before. `current` means the current session. A command without `--session-id` whose target flags name another device is still journaled in the current session |
| iOS input verbs | `backend`: `idb` or `axe` | also `xcuitest` when the runner served the input |
| iOS results (`ui *`, `screenshot`, flow events, journal summaries) | — | additive `ui_backend`, `input_backend`, `fallback_reason` and `geometry` when the invocation read the UI or sent input; flow events carry the first three |
| iOS compact nodes | — | additive `xcuitest_ref` on nodes from the runner's tree |
| `capabilities` | — | additive `--probe` (reads the current UI once). On iOS a UI read in the last 60 s refines `ui.accessibility` (`degraded` with the `fallback_reason` as reason); while the runner serves the session, `ui.input` is `available` after a runner input in the last 60 s, else `degraded`. Without such a read the installed-tooling values are unchanged |
| Flow preflight | — | a flow requiring `ui.accessibility` or `ui.input` on iOS reads the UI once first; a failed read leaves the tooling values to decide |
| Session record | — | additive `ui_backend_preference` and `ui_observation` |
| Machine state | — | `$AUTONOM_HOME/xcuitest/` (or `~/.autonom/xcuitest/`): cached runner builds and per-simulator runner state, mode `0700`; the process registry gains rows of kind `xcuitest` with a signature and process group |
| `session stop` teardown | — | an `xcuitest` action first: the runner is asked to stop and, if it does not exit, ended through its registry row; once it has exited, this run's result bundle, any older ones of that Simulator and the runner log are deleted, while a newer runner started concurrently and still alive keeps its own bundle and log (a new runner deletes the bundles an ended runner left; a runner that never got ready deletes its bundle and the runner log) |
| Error codes | — | `session_stopped`, `session_target_mismatch`, `display_geometry_unavailable`, `ui_action_uncertain`, `app_id_required`, `ui_bridge_in_use`, `xcuitest_unavailable`, `xcuitest_build_failed`, `xcuitest_start_failed`, `xcuitest_timeout`, `xcuitest_failed`, `stale_ui_element`, `no_focused_field` (the last three come from the runner) |

Behaviour changes:

- An iOS tap, long press, double tap or swipe whose screen size cannot be
  established is still dispatched unguarded on the idb/AXe route, as before
  (Canvas taps, sessions without `--app-id`, a zero root frame under
  `--ui-backend idb`). Only while the XCUITest runner serves the session is
  such input refused with `display_geometry_unavailable` before anything is
  sent: the runner maps coordinates in the points of the screen it reports.
  The guard's screen size no longer falls back to the widest child extent; a
  tree without a usable root frame is measured from the Simulator device type
  (`capabilities.plist` on Xcode 27, `profile.plist` before) and a screenshot
  instead, and is left unguarded when that fails too.
- With `auto`, an app session whose idb tree is empty, has no usable root
  frame, or fails is served by the XCUITest runner when full Xcode is
  present, instead of returning the empty tree or the idb error.


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
- Supported interpreter: Python >= 3.11 (`MIN_PYTHON` in `scripts/autonom.py`).
  CI tests 3.11 on pull requests and 3.14 on `main`. Raising it is a breaking
  change recorded here and in `CHANGELOG.md`. An older interpreter gets a
  `tool_missing` envelope (exit 2), never a traceback.

## Forward commitments

- The Flow DSL will add a `failure_class` field
  (`test_failure` | `flow_definition` | `infrastructure`) to flow-verb
  results and error envelopes, plus exit code `1` for *test* failures of
  `flow run` (following the `doctor --strict` precedent). Both changes are
  **additive** — existing envelope keys, codes, and exit semantics are
  unchanged.
