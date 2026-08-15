# Autonom capability matrix

Target: **universal mobile test/debug harness for AI agents** (Android + iOS).

Legend: ✅ shipped · ⚠️ partial · 🔜 planned · ❌ not planned for near term

| Capability | Android | iOS Simulator | Notes |
| --- | --- | --- | --- |
| Agent-portable skills (Codex / Claude / Grok) | ✅ | ✅ | install via marketplace or `install_skills.sh` |
| Unified device listing | ✅ | ✅ | `autonom devices`; each entry has a `running` flag |
| Boot / shut down a target | ✅ | ✅ | `devices boot --avd`/`--udid`, `devices shutdown`; refuses hardware |
| Bootable AVD discovery | ✅ | — | `devices` reports an `avds` array on Android |
| Explicit multi-target selection | ✅ | ✅ | `--platform` / `--target`; `--serial` and `--udid` are aliases |
| Environment diagnosis | ✅ | ✅ | `autonom doctor` |
| Session + artifact dirs | ✅ | ✅ | machine-global `~/.autonom/sessions/<id>/`; `autonom session *` |
| Session journal (actions + notes) | ✅ | ✅ | `journal.ndjson`; `autonom journal` / `note`; secret-safe |
| Boot / install / launch / terminate | ✅ | ✅ | simulator boots automatically on session start |
| Clear app data | ✅ | ⚠️ | Android `pm clear`; iOS uninstall+reinstall, or `--strategy privacy` for permissions only |
| Compact UI / accessibility tree | ✅ | ✅ | UIAutomator; idb `describe-all` |
| Semantic find / tap | ✅ | ✅ | text, desc/label, resource-id / accessibility identifier |
| Gestures | ⚠️ | ⚠️ | tap/swipe on both; `ui pinch\|rotate\|shake` have no backend on either and are refused — see below |
| Text and hardware keys | ✅ | ✅ | Android `KEYCODE_*`; iOS `HOME`/`LOCK`/`SIRI`/`SIDE_BUTTON` |
| Screenshot | ✅ | ✅ | iOS uses `simctl`, so it works without idb |
| Screenshot provenance | ✅ | ✅ | metadata embedded in the PNG; shots taken under an active mock are flagged `screenshot_shows_mocked_data` |
| Screenshot index / browse | ✅ | ✅ | `autonom shots list [--task --grep --mocked-only]`, `shots show <path>` |
| Screen recording artifact | ✅ | ✅ | `autonom record start\|stop` |
| Logs | ⚠️ | ⚠️ | logcat; `log stream`/`log show` with a bundle predicate |
| Crash reports | ⚠️ | ✅ | Android: crash logcat buffer; iOS: idb crash store |
| Deep links | ✅ | ✅ | `autonom open <url>` |
| Permissions | ✅ | ✅ | `pm grant/revoke/reset`; `simctl privacy` |
| Simulated location | ⚠️ | ✅ | set: iOS simctl / Android emulator `geo fix`; `location get` reads the fix on Android (iOS has no read-back) |
| Media library seeding | ✅ | ✅ | `autonom media add` |
| App-container file access | ✅ | ✅ | `autonom file ls\|pull`, confined to the container |
| Remote target host | — | ✅ | idb client can drive a companion on another Mac |
| Emulator browser mirror | ✅ | ❌ | `android-emulator-browser` |
| Network capture (HTTP/HTTPS) | ✅ | ⚠️ | mitmproxy, loopback-only, consent-gated |
| Response mocking | ✅ | ⚠️ | exact URL or glob + method/host; first enabled rule wins |
| Persistent mock registry | ✅ | ✅ | machine-level, survives restarts; full CRUD; reported by `doctor` |
| Process reaping | ✅ | ✅ | `processes` / `cleanup`, machine-wide; finds orphans by signature when the registry is lost |
| HAR export | ✅ | ✅ | HAR 1.2, credentials redacted |
| Device proxy attach | ✅ | ⚠️ | emulator via `10.0.2.2`; iOS per-process env, not `URLSession` |
| Credential redaction at capture | ✅ | ✅ | headers and body fields masked before writing |
| Flutter debug/test skills | ✅ | ⚠️ | iOS Flutter boundary partial |
| Native Android / Compose debug + perf skills | ✅ | — | |
| Native iOS project skills | — | ✅ | `ios-project-setup`, `ios-debugger-agent` |
| React Native skills | 🔜 | 🔜 | |
| Flow DSL: check / fmt / list | ✅ | ✅ | strict YAML subset, exact-match selectors by default, positioned errors; `docs/FLOW.md` |
| Flow DSL: run | ✅ | ✅ | polling assertions, single-fire mutations, `failure_class` + exit 1 for test failures, per-run `events.ndjson`; iOS `eraseText` dispatches HID backspace but is unproven on a real simulator |
| Flow DSL: runFlow / tags / hooks execution | ✅ | ✅ | subflows inline with inherited appId, isolated `onFlowComplete`, `when:` conditions, tag-filtered directory suites, evidence policy |
| Flow DSL: Session → Flow compiler | ✅ | ✅ | `flow create --from-session` — proven selectors reused, secrets become `${SECRET_n}`, coordinate taps refuse to compile |
| PR Proof (local) | ✅ | ✅ | `proof --base` — covers-globs + pull-request tags select the suite; verdicts pass/fail/not_covered/blocked/inconclusive, never upgraded |
| Atlas-lite: observed screens/transitions graph | ✅ | ✅ | `atlas update|show|coverage|paths|export|diff`; fingerprints ride in run events; observed-only, unknown stays unknown |
| Evidence: run manifest + HTML/JUnit reports | ✅ | ✅ | `report build|open|export`; self-contained HTML (CSP, inline screenshots), JUnit for CI, failure log window |
| Flow DSL: Maestro Core Profile import/export | ✅ | ✅ | `flow import`/`flow export --format maestro`; outside-profile constructs refuse with `unsupported_flow_command` |
| Live session outputs catalog | ✅ | ✅ | `session outputs` — registered `streams[]` + directory scan, `abs_path`/`shell_hint` for `tail -f` |
| Live follow (session files / device logs) | ✅ | ✅ | `logs follow` — NDJSON lines, always bounded by `--max-seconds`/`--max-lines`; `journal --follow` for the timeline |
| Network requests list | ✅ | ✅ | `network requests list --max N --since-id F` |
| Network requests follow | ✅ | ✅ | `network requests follow` — polls the store, emits only new flows as NDJSON |
| Metrics snapshot (memory/CPU summary) | ✅ | ✅ | `metrics snapshot` — Android meminfo/proc/cpuinfo vs iOS **host** `ps`+container size; `metric_semantics` + `limitations` name the difference, never comparable 1:1 |
| Metrics series (directional growth) | ✅ | ✅ | `metrics series` — live capture or `--from-dir`; leads are directional only, never called a leak |
| Memory capture pack / HPROF | ⚠️ | 🔜 | Android skill helpers; CLI in Phase 4 |
| Performance traces (simpleperf / xctrace) | ⚠️ | 🔜 | Android skill scripts; iOS xctrace CLI in Phase 4 |
| Frame stats (gfxinfo / Flutter timings) | ⚠️ | 🔜 | Partial via skills; unified CLI in Phase 4 |
| Flutter VM Service (widget tree, heap) | 🔜 | 🔜 | Phase 4.4 optional; frames summary earlier |
| XCUITest execution | — | 🔜 | Separate from metrics |
| Optional MCP wrapper | 🔜 | 🔜 | CLI is source of truth first |

### `ui pinch|rotate|shake` have no backend

Neither platform can perform them. Android's `input` cannot express them, and
idb has no such command either — `idb ui` accepts only `describe-all`,
`describe-point`, `tap`, `button`, `text`, `key`, `key-sequence`, and `swipe`
(verified against fb-idb 1.1.7). Both platforms therefore refuse the three verbs
with `unsupported_on_platform` and a hint, rather than dispatching something the
tool will reject.

Until 0.15.1 the iOS path did dispatch them, and every real machine answered
with `backend_failed` wrapping idb's argparse usage text plus a hint pointing at
`doctor` — a tool that was working fine. The suite missed it because the fake
idb returned 0 for any argv: it proved *what was dispatched*, never *that the
dispatch names a command idb has*. The fake now carries idb's real command
surface and refuses anything outside it.

Use `ui swipe` for anything reachable by a drag. Rotation and shake need a hand
on the Simulator window (Device > Rotate).

## CLI surface

Every leaf command also accepts the target flags
`--platform android|ios`, `--target`, `--serial`, `--udid`, and the tool
overrides `--adb`, `--simctl`, `--idb`, `--idb-host`, `--idb-port`, before or
after the verb.

```bash
autonom version
autonom devices [list] [--platform android|ios]
autonom devices boot [--avd NAME | --target ID] [--no-wait] [--timeout S] [--emulator PATH]
autonom devices shutdown [--target ID]
autonom doctor [--strict] [--mitmdump PATH]

autonom session start [--app-id ID] [--install PATH] [--launch [ID]] [--activity C] [--log-stream]
autonom session show|stop
autonom session outputs [--session-id ID]
autonom session launch <app-id> [--activity C] [--arg A] [--setenv K=V]
autonom session force-stop|uninstall <app-id>
autonom session clear <app-id> [--strategy auto|reinstall|privacy]

autonom ui tree [--dump FILE] [--all] [--max-depth N] [--max-nodes N]
autonom ui find [--text|--desc|--resource-id|--class-name|--package|--role] [--mode exact|contains|regex]
                [--case-sensitive] [--index N] [--clickable B] [--enabled B] [--all] [--dump FILE]
autonom ui tap [selector flags] | [--x X --y Y] [--duration MS]
autonom ui swipe --from X,Y --to X,Y [--duration S]
autonom ui pinch --at X,Y [--scale F] | ui rotate | ui shake   # iOS only
autonom ui type <text> [--sensitive]
autonom ui key <keycode>

autonom flow check <path>
autonom flow fmt <path> [--write] [--check] [--diff]
autonom flow list [path]
autonom flow create --from-session <ID> [--out PATH] [--name N] [--task T]
autonom flow import <path> [--out PATH]
autonom flow export <path> [--format maestro] [--out PATH]
autonom flow run <path> [--include-tag TAG] [--exclude-tag TAG] [--env KEY=VALUE]
                 [--secret NAME] [--default-timeout-ms N] [--events] [--dry-run]

autonom proof --base <REF> [--head REF] [--repo PATH] [--flows DIR] [--out DIR]
              [--env KEY=VALUE] [--secret NAME]

autonom atlas update [--session ID] [--app-id ID]
autonom atlas show [--app-id ID]
autonom atlas coverage [--app-id ID]
autonom atlas paths --from <SCREEN> --to <SCREEN> [--app-id ID]
autonom atlas export --out <PATH> [--app-id ID]
autonom atlas diff --base <PATH> [--head PATH] [--app-id ID]

autonom report build [--session ID] [--run ID]
autonom report open [--session ID] [--run ID]
autonom report export [--session ID] [--run ID] [--format html|junit] [--out PATH]

autonom screenshot [--label L] [--task T] [--out PATH]
autonom shots list [--task T] [--grep P] [--mocked-only] [--max N]
autonom shots show <path>
autonom record start [--name N] | record stop
autonom note add <text> [--task T] [--tag T] [--author A] | note list [--task --grep --max]
autonom journal [--kind action|note] [--verb V] [--task T] [--grep P] [--max N]
                [--follow] [--session-id ID] [--from-start] [--max-seconds N] [--max-lines N]

autonom metrics snapshot [--app-id ID] [--label L] [--task T] [--out PATH]
autonom metrics series [--app-id ID] [--label L] [--task T] [--out PATH] [--count N]
                       [--interval S] [--min-growth-kb N] [--from-dir DIR] [--glob G]
autonom metrics list-presets

autonom logs tail [--package ID] [--since S] [--max-lines N] [--grep P]
autonom logs follow [--source SRC | --path P] [--session-id ID] [--package ID] [--from-start]
                    [--max-seconds N] [--max-lines N] [--grep P] [--poll-ms N]
autonom crash list [--app-id ID] | crash show <name>
autonom open <url>
autonom permissions <grant|revoke|reset> <service> [app-id]
autonom location set <LAT,LON> | location get | location clear
autonom media add <path>
autonom file ls [remote] [--app-id ID] | file pull <remote> [--app-id ID] [--out PATH]

autonom network start --i-understand-mitm [--port N] [--capture-bodies] [--mitmdump PATH]
                      [--ignore-hosts REGEX] [--intercept-connectivity-checks]
autonom network attach --i-understand-mitm [--install-ca] [--no-network-cycle]
autonom network detach|stop|status
autonom network requests list [--host --method --status --path --since --mocked --max --since-id]
autonom network requests follow [--host --method --status --path --mocked] [--interval S]
                                [--max N] [--max-seconds N] [--from-start]
autonom network requests show <id> [--full]
autonom network mock add [--url U | --match GLOB] [--method M] [--host H] [--status N]
                         [--header 'K: V'] [--json BODY | --body-file PATH] [--note N]
autonom network mock update <id> [--url U | --match GLOB] [--method M] [--host H] [--status N]
                                 [--header 'K: V'] [--json BODY | --body-file PATH] [--note N]
autonom network mock list [--all] | show <id> | remove <id> | clear
autonom network mock enable [<id>|--all] | disable [<id>|--all]
autonom network export [--har PATH]

autonom processes
autonom cleanup [--dry-run] [--all]
```

Every command prints JSON. Expected failures print
`{"ok": false, "error_code": "...", "error": "...", "hint": "..."}` on stderr with
exit code 2, so an agent can branch on `error_code` rather than parse prose.
`doctor` is the exception: it exits 0 even when tools are missing unless
`--strict` is passed, because a diagnostic that fails is useless in a pipeline.

## Environment overrides

| Variable | Effect |
| --- | --- |
| `AUTONOM_HOME` | Overrides both state roots: sessions land in `$AUTONOM_HOME/sessions`, registries and the mitmproxy confdir directly beneath it |
| `XDG_STATE_HOME` | Machine state root when `AUTONOM_HOME` is unset (else `~/.local/state/autonom`) |
| `AUTONOM_ADB`, `AUTONOM_SIMCTL`, `AUTONOM_IDB`, `AUTONOM_EMULATOR`, `AUTONOM_MITMDUMP` | Binary paths, equivalent to the matching flag |
| `AUTONOM_IDB_COMPANION` | `host:port` of an idb companion on another Mac |
| `AUTONOM_PREFIX`, `AUTONOM_BIN_DIR` | Installer only: bundle home and the directory `autonom` is linked into |
| `AUTONOM_REQUIRE_SHELLCHECK` | Dev tooling only: `run_checks.sh` fails instead of skipping the shell lint when shellcheck is missing (set by CI) |

## Evidence ladder (unchanged)

code → unit/widget → integration on explicit target → screenshot + UI tree + logs →
profile/memory/network → before/after replay.

## Limitations closed

What each earlier limitation cost, and what replaced it.

| Earlier limitation | Autonom response |
| --- | --- |
| Kotlin/Compose-first scope | Six Flutter-specific skills plus hybrid routing |
| Codex-only packaging | Portable skills + one-command `install.sh` for Codex, Claude, Grok, generic agents |
| Static toolchain snapshot | Repository/local inspection with no “latest” assertion |
| Screenshot polling browser | H.264 + ffmpeg MJPEG path, persistent fallback, status and reconnect |
| Android-only device control | One verb set over Android and the iOS Simulator, with a shared compact node schema |
| Unauthenticated input bridge | Random token, localhost-only bind, allowlisted input, body limits |
| Exact text-only UI targeting | text/semantics/id/class/package, exact/contains/regex, waits and duplicate control |
| Directional memory capture only | Structured artifacts plus multi-capture trend analysis, while retaining proof rules |
| Limited executable validation | Python and Node tests, fake adb/simctl/idb backends, a recorded contract golden, a bare-host sweep, a TTY guard, and a doc-drift check — all run locally by `./scripts/run_checks.sh` |
| Generic Compose advice for Flutter apps | Flutter architecture, widgets, tests, performance, memory, platform, and release workflows |
| Project-local artifacts, invisible from elsewhere | Machine-global `~/.autonom/`: the session, its mocks, and orphaned processes are found and reaped from any directory |
| No record of what an agent did | Append-only `journal.ndjson` — every verb, its scrubbed argv, the result, and the failures, plus agent notes |
| A screenshot that silently showed mocked data | Provenance embedded in the PNG; captures taken under an active rule are flagged `screenshot_shows_mocked_data` |

The harness intentionally does not claim that trend analysis proves a memory
leak or that a browser preview proves performance. Those require retained-path
or repeatable runtime evidence.

## Competitive posture

| Class | Examples | Autonom stance |
| --- | --- | --- |
| Device MCP | mobile-mcp, Appium MCP, Maestro MCP | Interop later; Autonom wins on skills + evidence loop + multi-agent install |
| Code pattern skills | PromptSpace-style RN/Flutter/Swift packs | Expand domain skills; already strong on Flutter/Android |
| Network tools | mitmproxy, HTTP Toolkit, Proxyman | Wrap OSS (mitmproxy) behind `autonom network` |

See `docs/ARCHITECTURE.md` and `docs/plans/` for phased delivery (network, MCP).
