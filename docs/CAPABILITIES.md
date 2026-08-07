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
| Gestures | ⚠️ | ✅ | Android: tap/swipe; iOS: + pinch, rotate, shake |
| Text and hardware keys | ✅ | ✅ | Android `KEYCODE_*`; iOS `HOME`/`LOCK`/`SIRI`/`SIDE_BUTTON` |
| Screenshot | ✅ | ✅ | iOS uses `simctl`, so it works without idb |
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
| Live session outputs catalog | 🔜 | 🔜 | Phase 4 — `session outputs` + paths for `tail -f`; see `docs/plans/PHASE_4_METRICS.md` §2L |
| Live follow (session files / device logs) | 🔜 | 🔜 | Phase 4 — `logs follow` (NDJSON, bounded) |
| Network requests list | ✅ | ✅ | `network requests list --max N`; follow/poll in Phase 4 |
| Network requests follow | 🔜 | 🔜 | Phase 4 — poll new flows as NDJSON |
| Metrics snapshot (memory/CPU summary) | 🔜 | 🔜 | Phase 4 — `autonom metrics snapshot` |
| Metrics series (directional growth) | 🔜 | 🔜 | Phase 4 — same plan; Android meminfo series exists as skill script today |
| Memory capture pack / HPROF | ⚠️ | 🔜 | Android skill helpers; CLI in Phase 4 |
| Performance traces (simpleperf / xctrace) | ⚠️ | 🔜 | Android skill scripts; iOS xctrace CLI in Phase 4 |
| Frame stats (gfxinfo / Flutter timings) | ⚠️ | 🔜 | Partial via skills; unified CLI in Phase 4 |
| Flutter VM Service (widget tree, heap) | 🔜 | 🔜 | Phase 4.4 optional; frames summary earlier |
| XCUITest execution | — | 🔜 | Separate from metrics |
| Optional MCP wrapper | 🔜 | 🔜 | CLI is source of truth first |

## CLI surface

```bash
autonom devices [list] [--platform android|ios]
autonom devices boot [--avd NAME | --target ID] [--no-wait] [--timeout S]
autonom devices shutdown [--target ID]
autonom doctor [--strict]
autonom session start|show|stop|launch|force-stop|clear|uninstall
autonom ui tree|find|tap|swipe|pinch|rotate|shake|type|key
autonom screenshot
autonom record start|stop
autonom logs tail
autonom crash list|show
autonom open <url>
autonom permissions grant|revoke|reset <service> [app-id]
autonom location set|get|clear
autonom media add <path>
autonom file ls|pull
autonom note add <text> [--task --tag] | note list
autonom journal [--kind action|note] [--verb V] [--task T] [--grep P]
autonom network start|stop|status|attach|detach
autonom network requests list|show
autonom network mock add|list|show|update|enable|disable|remove|clear
autonom network export --har <path>
autonom processes
autonom cleanup [--dry-run] [--all]
```

Every command prints JSON. Expected failures print
`{"ok": false, "error_code": "...", "error": "...", "hint": "..."}` on stderr with
exit code 2, so an agent can branch on `error_code` rather than parse prose.

## Evidence ladder (unchanged)

code → unit/widget → integration on explicit target → screenshot + UI tree + logs →
profile/memory/network → before/after replay.

## Competitive posture

| Class | Examples | Autonom stance |
| --- | --- | --- |
| Device MCP | mobile-mcp, Appium MCP, Maestro MCP | Interop later; Autonom wins on skills + evidence loop + multi-agent install |
| Code pattern skills | PromptSpace-style RN/Flutter/Swift packs | Expand domain skills; already strong on Flutter/Android |
| Network tools | mitmproxy, HTTP Toolkit, Proxyman | Wrap OSS (mitmproxy) behind `autonom network` |

See `docs/ARCHITECTURE.md` and `docs/plans/` for phased delivery (network, MCP).
