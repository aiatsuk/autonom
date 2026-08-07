# Architecture

Autonom is a **universal mobile test and debug harness for AI agents**. It ships
as portable `SKILL.md` skills plus a dependency-light **CLI control plane**.
The core design is **routing first, evidence second**.

## Design goals

1. **Agent-portable** — one skill body for Codex, Claude, Grok, and other
   skill-compatible runtimes.
2. **Stack-honest** — detect Flutter, native Android, native iOS, or hybrid
   before loading domain skills.
3. **Accessibility-first** — compact UI trees for structure; screenshots for
   visual confirmation; no mandatory paid vision API.
4. **CLI as source of truth** — skills call `scripts/autonom.py …` JSON APIs;
   optional MCP later wraps the same verbs.
5. **Evidence-backed** — prefer measured artifacts over narrative certainty.
6. **Dependency-light** — stdlib Python, Node without npm for the bridge, shell
   helpers; OS tools (adb, simctl, idb, mitmproxy) invoked at runtime and always
   optional at import time.

## Layers

1. `project-router` classifies the repo and loads the smallest skill set.
2. Planning/code skills constrain architecture and implementation.
3. Runtime skills (`mobile-session`, `mobile-screen`, debugger/perf/memory)
   select one explicit target and gather artifacts.
4. **Autonom CLI** normalizes device I/O into deterministic JSON.
5. Helpers (UI Automator parsers, browser bridge, meminfo/frame tools) back the CLI
   and specialized skills.
6. `./scripts/run_checks.sh` validates manifests, frontmatter, Python/Node syntax,
   and the unit suite. It is run locally; this repository ships no CI
   configuration, so "green" means a clean local run.

## Control plane

```text
Host agent  →  skills  →  scripts/autonom.py  →  adb / xcrun simctl / idb / (later) mitmproxy
                              ↓
                     .autonom/<session>/artifacts
```

### Module map

```text
scripts/autonom.py            argparse surface and JSON emit only; no platform branching
scripts/autonom_lib/
  platform.py                 Target identity, resolution precedence, unified device listing
  errors.py                   AutonomError + the stable error_code vocabulary
  session.py                  schema v2 records, v1 in-memory upgrade, teardown registry
  selector.py                 shared selector matching and duplicate policy
  ui.py                       platform dispatch + the compact node schema
  ui_android.py / adb.py      UI Automator parsing and adb actuation
  ui_ios.py / ios_idb.py      accessibility tree parsing and idb actuation
  ios_simctl.py               simulator lifecycle, screenshots, logs, device state
  device_state.py             deep links, permissions, location, media, crashes, files, recording
  screenshot.py / logs.py     per-platform dispatch
  doctor.py                   toolchain, capabilities, orphans
```

Two rules carry the design: `autonom.py` never branches on platform, and
`ios_idb.py` is the only module that knows idb's command line — so an Xcode
upgrade that breaks idb has a single-file blast radius.

### Verbs

| Verb | Purpose |
| --- | --- |
| `devices`, `doctor` | discover targets and diagnose the machine |
| `session *` | start/show/stop, launch/clear/force-stop/uninstall, artifact dirs |
| `ui tree\|find\|tap\|swipe\|pinch\|rotate\|shake\|type\|key` | understand and control the screen |
| `screenshot`, `record start\|stop` | visual evidence |
| `logs tail`, `crash list\|show` | textual evidence |
| `open`, `permissions`, `location`, `media`, `file` | drive device state |
| `network *` | consent-gated HTTP(S) capture, mock, HAR |
| `session outputs`, `logs follow`, `network requests follow` | **planned (Phase 4)** — live session watch |
| `metrics *` | **planned (Phase 4)** — memory/CPU/frames/traces; see `docs/plans/PHASE_4_METRICS.md` |

## Compact node schema

The single most important contract: an iOS node and an Android node are
indistinguishable in shape, which is what lets one skill body drive both.

```json
{"ref": "n5", "role": "button", "text": null, "desc": "General",
 "resource_id": "com.apple.settings.general", "class": "Button", "package": null,
 "bounds": [16, 380, 386, 432], "clickable": true, "enabled": true,
 "focusable": false, "scrollable": false, "selected": false, "checked": false,
 "depth": 0}
```

iOS bounds are **points**, matching what idb's tap accepts. A computed tap outside
the target's reported screen rectangle is refused rather than dispatched, because a
point/pixel mix-up otherwise "succeeds" while landing in the wrong place.

## Packaging surfaces

| Surface | Path | Consumers |
| --- | --- | --- |
| Portable skills | `plugins/autonom/skills/*/SKILL.md` | All agents |
| CLI | `scripts/autonom.py`, `scripts/autonom_lib/` | Skills + humans |
| Codex marketplace | `.agents/plugins/marketplace.json` | Codex |
| Codex plugin manifest | `plugins/autonom/.codex-plugin/plugin.json` | Codex |
| Installer | `scripts/install_skills.sh` | Claude, Grok, generic |
| Validation | `scripts/validate_plugin.py`, `scripts/run_checks.sh` | local |

## Testing posture

- **Contract golden** — every Android response's key set was recorded from 0.4.0
  before the platform refactor; a renamed key fails the build even though
  hand-written assertions would still pass.
- **Fake backends** — `tests/fakes/fake_{adb,simctl,idb}.py` record their argv, so
  "what did we actually execute?" is an oracle rather than a claim.
- **Bare-host sweep** — every verb run with an empty `PATH` must fail with one
  machine-readable `error_code` and no traceback.
- **Device-backed** — real simulator or emulator runs are manual and evidenced by
  before/after artifacts, never by exit codes alone.

## Flutter-first boundary (current domain pack)

Dart/widget work remains in Flutter skills. Android skills are added only for
Gradle, manifests, Kotlin, platform channels, permissions, app links,
notifications, platform views, process performance, or native memory. iOS skills
cover project layout and simulator debugging; SwiftUI code-pattern skills and
React Native are planned beside this boundary, not as a rewrite of the harness
contract.

## Evidence ladder

- code inspection;
- narrow unit/widget/native test;
- explicit-target integration flow;
- screenshot + semantics/UI tree + logs;
- profile/memory/network artifact from an exact flow;
- equivalent before/after replay.

Claims are separated into measured facts, code-backed findings, hypotheses, and
remaining uncertainty.

## Security boundary

Browser bridge and the future network proxy bind to localhost by default, use
tokens or explicit user confirmation for privileged setup, limit input surfaces,
and must never be exposed publicly. Do not install system CAs or MITM traffic
without explicit operator consent for that exact action. App-container file access
is confined to the container and never echoes file contents. The MITM CA private
key lives in a machine-level store outside session artifacts; only the certificate
is published into a session. Never print secrets,
keystores, or `.env` values.

## Related docs

- `docs/CAPABILITIES.md` — shipped vs planned matrix
- `docs/INSTALL.md` — multi-agent install and per-platform prerequisites
- `docs/USAGE.md` — prompts and workflows
- `docs/plans/` — phase plans and spike verdicts
