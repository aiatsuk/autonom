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
   and the unit suite. The same script runs locally and in GitHub Actions:
   `checks` on every PR and push to main (ubuntu + macOS, pinned Python and
   Node, shellcheck required), `android-smoke` on main (a real API-30 emulator
   driven through the CLI), and `release` on `v*` tags. "Green" means CI green.

## Control plane

```text
Host agent  →  skills  →  scripts/autonom.py  →  adb / xcrun simctl / idb / mitmdump
                              ↓
                     ~/.autonom/sessions/<id>/
```

### State model

Nothing lives in the project directory. Two machine-global roots, both
overridable by `AUTONOM_HOME`:

| Root | Default | Holds |
| --- | --- | --- |
| Session store | `~/.autonom/sessions/<id>/` | `session.json`, `journal.ndjson`, shots, trees, logs, network flows, recordings, crashes, pulled files |
| Machine state | `$XDG_STATE_HOME/autonom`, else `~/.local/state/autonom` | mock registry, process registry, mitmproxy confdir (CA key, mode `0700`) |
| Per-app knowledge | `~/.autonom/apps/<package>/` | `mobile-memory` overlays and flow runbooks |

The consequence that matters: a session started in one directory is found from
any other, a proxy orphaned by a crashed shell is still reapable, and a mock
rule added days ago is still in force the moment a proxy starts — which is why
`doctor` and `network start` both report the registry unprompted.

The legacy project-local `.autonom/` layout survives only as a library-level
opt-in (`artifacts_root(cwd=…)`), used by tests and by anyone who deliberately
wants a run's artifacts beside the code. No CLI verb selects it.

### Module map

```text
scripts/autonom.py            argparse surface, JSON emit, and the journal choke point
scripts/autonom_lib/
  platform.py                 Target identity, resolution precedence, unified device listing
  errors.py                   AutonomError + the stable error_code vocabulary
  session.py                  schema v2 records, v1 in-memory upgrade, teardown registry
  journal.py                  append-only journal.ndjson: every verb, scrubbed argv, notes
  consent.py                  the consent gate: per-invocation flag + typed phrase, never cached
  selector.py                 shared selector matching and duplicate policy
  ui.py                       platform dispatch + the compact node schema
  ui_android.py / adb.py      UI Automator parsing and adb actuation
  ui_ios.py / ios_idb.py      accessibility tree parsing and idb actuation
  ios_simctl.py               simulator lifecycle, screenshots, logs, device state
  emulator.py                 AVD discovery, emulator boot and kill
  device_state.py             deep links, permissions, location, media, crashes, files, recording
  screenshot.py / logs.py     per-platform dispatch; shots carry provenance metadata in the PNG
  follow.py                   bounded NDJSON follows: file tail, device-log stream, store poll
  metrics/
    meminfo.py                dumpsys meminfo / proc status / cpuinfo parsers
    process.py                pid resolution with sources_tried on failure
    snapshot.py               per-platform load summary; iOS is host accounting, and says so
    series.py                 first/last/delta/slope math; directional leads, never "leak"
    presets.py                which heavy profilers this host can run
    android_memory.py         evidence pack: metadata+meminfo+proc+gfxinfo+HPROF, analyze
    frames.py                 gfxinfo reset/capture (best-effort parse) + Flutter timings
    trace.py                  simpleperf / gfxinfo-flow / xctrace presets → artifacts
  processes.py                machine-wide process registry, orphan detection, reaping
  doctor.py                   toolchain, capabilities, session, orphans
  paths.py                    locate the bundled skill helper scripts
  flow/
    parser.py                 strict YAML-subset parser: text -> positioned nodes, one error code
    schema.py                 typed model: command registry, selector surface, failure classes
    validator.py              runFlow graph loading, workspace containment, cycle refusal
    canonical.py              deterministic emitter behind `flow fmt`
    executor.py               `flow run`: pre-flight, polling engine, single-fire mutations,
                              runFlow composition, isolated cleanup hooks, evidence policy
    events.py                 versioned run events: NDJSON per run + slim journal bridge
    selectors.py              flow selector -> selector.py translation (never a reimplementation)
    conditions.py             `when:` evaluation (platform/visible/notVisible/envEquals, AND)
    compiler.py               Session -> Flow: journal + action details -> canonical YAML
    maestro.py                Maestro Core Profile import/export, faithful or refused
    report.py                 run manifest -> self-contained HTML + JUnit renderers
  atlas/
    fingerprint.py            volatility-resistant screen identity (structure/state hashes)
    graph.py                  the observed graph: storage, ingestion, coverage, paths, diff
  proof.py                    PR proof: git diff -> covering suite -> honest verdict
  actions.py                  per-action detail records feeding the Session -> Flow compiler
  network/
    proxy.py                  mitmdump lifecycle, machine-level confdir, CA publication
    mitm_addon.py             in-proxy addon: record, redact, serve mocks
    store.py / har.py         flow storage and filtering; HAR 1.2 export
    mocks.py                  persistent mock registry, CRUD, glob matching
    redact.py                 credential masking, applied before anything is written
    device_proxy_android.py   emulator attach via 10.0.2.2, CA seeding, detach restore
    device_proxy_ios.py       per-process proxy env at launch, keychain CA, manual fallback
```

Two rules carry the design.

1. **Actuation is single-sourced per backend.** Every idb command line lives in
   `ios_idb.py`, every adb command line in `adb.py`, every simctl command line
   in `ios_simctl.py`, so an Xcode upgrade that breaks idb has a one-file blast
   radius. `doctor.py` is the deliberate exception: it probes `idb list-targets`
   itself, because a diagnostic must keep working when the wrapper does not.
2. **Platform knowledge stays below the CLI.** `autonom.py` still branches on
   `target.platform` where the *verb itself* differs between platforms — iOS has
   no `pm clear`, Android has no per-process launch environment, iOS attach is
   not automatable the way emulator attach is. Everything below that, from
   parsing to actuation, is dispatched by `ui.py` / `screenshot.py` / `logs.py`
   and never by the argparse layer.

### Verbs

| Verb | Purpose |
| --- | --- |
| `version`, `devices`, `doctor` | identify the CLI, discover targets, diagnose the machine |
| `session *` | start/show/stop, launch/clear/force-stop/uninstall, artifact dirs |
| `ui tree\|find\|tap\|swipe\|pinch\|rotate\|shake\|type\|key` | understand and control the screen |
| `screenshot`, `shots list\|show`, `record start\|stop` | visual evidence and its provenance |
| `note add\|list`, `journal` | the run's own record: what was done, what was concluded |
| `logs tail`, `crash list\|show` | textual evidence |
| `open`, `permissions`, `location`, `media`, `file` | drive device state |
| `network *` | consent-gated HTTP(S) capture, mock, HAR |
| `atlas update\|show\|coverage\|paths\|export\|diff` | the observed application graph, evidence-linked |
| `proof --base` | run the covering flow suite for a diff; pass/fail/not_covered/blocked/inconclusive |
| `report build\|open\|export` | self-contained HTML + JUnit from a run's manifest |
| `report build\|open\|export` | self-contained HTML + JUnit from a run's manifest |
| `flow check\|fmt\|list\|run` | validate, canonicalize, enumerate, and execute Flow v1 files (`docs/FLOW.md`); `run` polls assertions, fires mutations exactly once, and classifies failures |
| `processes`, `cleanup` | machine-wide reaping of what Autonom started |
| `session outputs`, `logs follow`, `network requests follow` | **planned (Phase 4)** — live session watch |
| `metrics *` | **planned (Phase 4)** — memory/CPU/frames/traces; see `docs/plans/PHASE_4_METRICS.md` |

Every verb except `note`, `journal`, and `version` passes through one journal
choke point in `main()`, so the timeline records the failures too — including
the ones the agent chose not to mention.

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
| Portable skills | `plugins/autonom/skills/*/SKILL.md` (24) | All agents |
| CLI | `scripts/autonom.py`, `scripts/autonom_lib/` | Skills + humans |
| Claude marketplace | `.claude-plugin/marketplace.json` | Claude Code |
| Codex marketplace | `.agents/plugins/marketplace.json` | Codex |
| Plugin manifests | `plugins/autonom/.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` | Claude, Codex |
| One-command installer | `install.sh` (+ `scripts/bootstrap.sh` for device tools) | everyone |
| Per-layer installers | `install_cli.sh`, `install_claude.sh`, `install_codex.sh`, `install_skills.sh` | Claude, Codex, Grok, generic |
| Validation | `scripts/validate_plugin.py`, `scripts/run_checks.sh` | local + CI (`.github/workflows/`) |
| Release | `scripts/build_release.sh`, `.github/workflows/release.yml`, `CHANGELOG.md` | tagged GitHub Releases |

The version in `scripts/autonom_lib/__init__.py` is the single source: the
validator fails the build when a plugin manifest disagrees with it.

## Testing posture

- **Contract golden** — every Android response's key set was recorded from 0.4.0
  before the platform refactor; a renamed key fails the build even though
  hand-written assertions would still pass.
- **Fake backends** — `tests/fakes/fake_{adb,simctl,idb}.py` record their argv, so
  "what did we actually execute?" is an oracle rather than a claim. The fake idb
  additionally carries the real tool's command surface and refuses anything
  outside it: while it accepted any argv, three `ui` gestures were dispatched as
  commands idb does not have and the suite stayed green for several releases.
- **Doc-drift check** — every verb and flag in `build_parser()` is compared
  against the CLI surface block in `docs/CAPABILITIES.md`, in both directions.
- **Bare-host sweep** — every verb run with an empty `PATH` must fail with one
  machine-readable `error_code` and no traceback.
- **TTY guard** — the suite is re-run with a stdin that claims to be a terminal
  and raises on read. A consent prompt that would block a developer's terminal
  fails here instead, which a headless run would never catch.
- **Environment hygiene guard** — the alphabetically-first test module snapshots
  `os.environ`, the alphabetically-last compares it; a test that mutates the
  environment without restoring it (`tests/env_isolation.py` is the sanctioned
  idiom) fails the suite instead of silently redirecting later tests to the
  operator's real `~/.autonom`.
- **Device-backed** — the `android-smoke` workflow drives a real emulator
  through the CLI on every push to main; deeper simulator/emulator runs remain
  manual and evidenced by before/after artifacts, never by exit codes alone.

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

The browser bridge and the network proxy bind to localhost — the proxy has no
flag to widen it — use tokens or explicit confirmation for privileged setup, and
must never be exposed publicly. Consent for MITM and CA installation is required
per invocation: a flag plus a typed phrase on a terminal, never cached, never
grantable by an environment variable or a prior run. App-container file access is
confined to the container and never echoes file contents. Credentials are masked
at capture time, so an archived artifact directory has never held them. The MITM
CA private key lives in the machine-level state root (mode `0700`) outside
session artifacts; only the certificate is published into a session, which also
keeps it stable across sessions so a device that trusted it once keeps working.
Autonom never changes host network settings — a test asserts `networksetup` and
`scutil` appear nowhere in the codebase. Never print secrets, keystores, or
`.env` values.

`SECURITY.md` carries the full model, including what teardown does and does not
restore.

## Related docs

- `docs/CAPABILITIES.md` — shipped vs planned matrix, CLI surface, limitations closed
- `docs/INSTALL.md` — multi-agent install and per-platform prerequisites
- `docs/USAGE.md` — prompts and workflows
- `docs/plans/` — phase plans and spike verdicts
- `SECURITY.md` — the enforced security model
