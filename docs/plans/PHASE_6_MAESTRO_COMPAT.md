# Phase 6: Maestro Core Profile v2 — near-full import compatibility

**Status:** in progress — 0.28.0 (import courtesy) shipped; 0.28.1–0.28.3 pending
**Goal:** run the overwhelming majority of real-world Maestro flows through Autonom — by widening the documented Core Profile from "toy flows" to every Maestro command and argument that can be implemented honestly under Autonom's invariants, and by letting `flow run` execute a Maestro file directly through transparent import. Scope is explicitly bounded: no JavaScript, no AI commands, no random input, no raw coordinates, no pixel assertions, no settle heuristics.
**Depends on:** `PHASE_5_FLOW_DSL.md` (Flow v1 language, executor, Maestro Core Profile v1 in 0.22.0); research: `docs/AUTONOM_PRODUCT_RESEARCH_REVYL_MAESTRO.md` §15 (versioned Core Profile commitment).
**Target versions:**

| Version | Theme |
|---|---|
| 0.28.0 | Import courtesy: flow-mapping parse, key aliases, header hooks, `flow run` on Maestro files |
| 0.28.1 | Engine-only commands: clipboard trio + variables, bounded `repeat`, `scroll`, inline `runFlow`, element-anchored swipe, argument parity |
| 0.28.2 | Device-state commands: `killApp`, `hideKeyboard`, airplane mode, `clearKeychain`, `travel`, recording, extended keys, launch permissions, `openLink` args |
| 0.28.3 | `launchApp.arguments` (post-spike), workspace suite config, compatibility table, closing review |

After this phase Autonom covers **36 of the 44 documented Maestro commands** (all except the 6 script/AI commands and the 2 pixel/settle commands; the 7 `inputRandom*` commands exist only in Maestro's source, are absent from its documented catalog, and are also refused), with argument-level exclusions listed in §0.1.

---

## 0. Why this phase exists

Maestro is the dominant YAML flow dialect; agents and humans both arrive with Maestro files and Maestro habits. Core Profile v1 (0.22.0) proved the honest-conversion model but covers too little in practice: the strict parser rejects the single most common real-world idiom (`tapOn: {text: X}`) as a raw `flow_parse_error`, and 19 commands sit in the import refusal table (`_UNSUPPORTED_HINTS`, `scripts/autonom_lib/flow/maestro.py:38-58`) even though most of them are cheap to support natively. Deep research against the Maestro 2.8.0 source (2026-08-17) established the split: of the 44 documented commands, 23 are covered, 13 are cheap honest additions, 2 are expensive (pixel diffing), and 6 (script/AI) are impossible under stdlib-only/local-first invariants — 23 + 13 = 36 after this phase. This phase ships the cheap 13 plus argument completeness, and makes the import boundary courteous instead of cryptic.

The compatibility posture stays exactly as committed in the research doc: a **versioned Core Profile**, never a parity promise. Everything outside the profile refuses loudly with `unsupported_flow_command` + hint; nothing imports silently and means something else.

### 0.1 Non-goals (explicit)

- **No JavaScript, ever**: `runScript`, `evalScript`, `assertTrue`, and `${...}` containing anything but a variable name stay refused (stdlib-only; the calling agent is the computation layer).
- **No AI commands**: `assertWithAI`, `assertNoDefectsWithAI`, `extractTextWithAI` are cloud-gated and probabilistic — refused.
- **No random input**: `inputRandomText` and the other six `inputRandom*` commands break replay by definition — refused; the host generates, `--env` injects.
- **No raw coordinates**: `point` selectors, `swipe` `start`/`end` forms stay refused (device-geometry-dependent).
- **No pixel/settle heuristics in this phase**: `assertScreenshot` and `waitForAnimationToEnd` are deliberately out of scope (expensive; a possible later `waitUntilSettled` design is not part of v2). `waitToSettleTimeoutMs` arguments refuse with a hint.
- **No implicit mutation retry**: `retryTapIfNoChange` and `waitUntilVisible` re-tap on `tapOn` refuse — exactly-once dispatch is a core guarantee.
- **No per-step app switching**: `launchApp`/`stopApp` with an inline `appId` different from the flow's stays refused (single-app session and consent model).
- **No cloud surface**: workspace config cloud keys (`baselineBranch`, `notifications`) and the cloud-only `platform.*.disableAnimations` flag refuse; no sharding.
- **No native-grammar loosening beyond §1.1**: anchors, aliases, block scalars, multi-line scalars stay refused everywhere.

### 0.2 Design principles

1. **Convert, never emulate.** A Maestro file executes only after honest conversion to canonical Flow v1; where semantics cannot be preserved (regex matching is bridged, ambiguity refusal is not relaxed), the difference is documented, not hidden.
2. **Refusal is a feature.** Every newly supported command deletes a hint from `_UNSUPPORTED_HINTS`; everything remaining gets a pointed, positioned hint.
3. **Platform honesty over Maestro fidelity.** Where Maestro silently no-ops (airplane mode on iOS, `clearKeychain` on Android, most iOS keys), Autonom refuses with `unsupported_on_platform`.
4. **Additive contract.** New commands, args, and error codes are add-only; the docs fences (`docs/FLOW.md` language surface, `docs/CAPABILITIES.md` CLI surface) gate both directions as always.

---

## 1. Slice 0.28.0 — import courtesy

### 1.1 Single-line flow mappings

`scripts/autonom_lib/flow/parser.py` gains an opt-in mode (`allow_flow_mappings=True`, used **only** by `import_flow`) accepting single-line `{key: value, ...}` mappings — bounded, no nesting of mappings inside mappings, existing scalar rules apply. The native grammar is unchanged: a hand-written Flow v1 file with `{...}` still refuses with `flow_parse_error` reason `flow_mapping`. This converts the most common real-world Maestro idiom (`tapOn: {text: "OK", index: 1}`) from a parse death into a working import or a courteous command-level refusal.

### 1.2 Import key aliases

Alias table applied during import only (canonical Flow v1 keeps single spellings): `timeout`→`timeoutMs`, `duration`→`durationMs`, `text`→`value` (inputText), `maxRetries`→`maxAttempts`, `charactersToErase`→`chars`, `element`→`selector` (scrollUntilVisible), `speed`→ documented translation (§3.5).

### 1.3 Header widening

`_CORE_HEADER` (maestro.py:36) grows: `onFlowStart` / `onFlowComplete` import into the native hook fields (command lists convert through the same dispatch; commands outside the profile refuse with position), `properties` imports verbatim, `url` refuses with a hint (no web target). Unknown header keys keep refusing — Maestro's silent `ext` swallowing is not reproduced.

### 1.4 `flow run` on Maestro files

`flow run <file>` detects a Maestro document (no `schema:` field in the header) and converts in memory through the same `import_flow` path before pre-flight; the run journal and `flow.run.started` event record `converted_from: maestro`. Refusals surface exactly as `flow import` would emit them. No new CLI verb; `flow check` and `flow fmt` gain the same detection. `--out`-less `flow import` behavior is unchanged.

## 2. Slice 0.28.1 — engine-only commands (no device substrate)

Research finding that shapes this slice: **Maestro's clipboard commands never touch the device.** `copyTextFrom` reads the matched node's text host-side (fallback text → hintText → accessibilityText), `setClipboard` assigns a host variable, `pasteText` types that variable via ordinary input (Orchestra.kt:1772-1804). Autonom mirrors this exactly — the OS clipboard is untouched, which the docs state explicitly.

### 2.1 Variable register

- `copyTextFrom: {selector, into: NAME, label}` — resolves the selector under the standard ambiguity rules, stores the node text into a run-scoped variable frame consumed by existing `${NAME}` interpolation. Without `into:` the value lands in the implicit `COPIED_TEXT` variable (Maestro-import default).
- `setClipboard: {value, into?, label}` — assigns a variable (default `COPIED_TEXT`); import maps Maestro's `setClipboard` here.
- `pasteText: {label}` — types the current `COPIED_TEXT` via the existing input path.
- Precedence: header `env` < runFlow `env` < `--env` < run-scope variables < `--secret`. A variable copied from a field the session marked sensitive propagates the `sensitive` flag into events and typed-text redaction.

### 2.2 Bounded `repeat`

`repeat: {times*, while?, commands*, label}` — `times` is **mandatory** (1–25), `while:` (`visible`/`notVisible` only) is an early-exit check before each iteration, every iteration's steps land in events/journal with an iteration index. `repeat` leaves `DEFERRED_COMMANDS`. Rationale for no `allowMutations` gate (unlike `retry`): `retry` re-executes after a *failure* and can mask flake; `repeat` is declared, unconditional, bounded iteration — deterministic by construction. Nested `repeat`/`retry`/`group` inside `repeat` refuses.

### 2.3 Command and argument completion

- `scroll` — alias for one upward directional swipe (both platforms; same geometry as `swipe: {direction: up}`).
- `swipe: {direction, from: <selector>}` — element-anchored swipe: resolve `from` under ambiguity rules, start at its center. Coordinate `start`/`end` forms keep refusing.
- `runFlow: {commands: [...]}` — inline anonymous subflow, same env-frame semantics as file subflows; `file` XOR `commands` enforced as in Maestro.
- `scrollUntilVisible.centerElement` — after visibility, bounded corrective micro-swipes to center the node.
- `tapOn.repeat`/`tapOn.delay` — deterministic N taps with fixed delay (doubleTapOn generalized).
- `eraseText` numeric shorthand; `inputText` numeric/bool scalar stringification on import.
- Selector: `containsDescendants` leaves the deferred list (any-depth descendant walk over the parent refs each snapshot carries; anchors keep the exactly-one rule).

## 3. Slice 0.28.2 — device-state commands

Substrate verified against Maestro 2.8.0 source and this Mac's `simctl` (2026-08-17). Every platform gap refuses with `unsupported_on_platform` — never a silent no-op.

| Command | Android | iOS Simulator |
|---|---|---|
| `killApp` | `am kill <pkg>` (background-state kill; documented distinction from `stopApp`/force-stop) | alias of existing terminate — documented |
| `hideKeyboard` | `input keyevent 4` (BACK) via existing press-key path | refuses (no simctl substrate; Maestro needs its XCTest runner) |
| `setAirplaneMode` / `toggleAirplaneMode` | API≥28 `cmd connectivity airplane-mode enable\|disable`, read-back with `settings get global airplane_mode_on` fallback; API<28 refuses (root-only broadcast) | refuses (Maestro itself warns "not available on iOS simulators") |
| `clearKeychain` | refuses | `simctl keychain <udid> reset` |
| `travel: {points, speed}` | engine loop over existing `setLocation` (emulator-only, already gated) with distance/speed pacing | same loop; optionally delegate to `simctl location start --speed` waypoint interpolation (open decision D-o3) |
| `startRecording` / `stopRecording` | reuse `device_state.record_start/record_stop` (existing `screenrecord` + SIGINT + pull); double-start refuses via existing recording-active error | same reuse (`simctl io recordVideo`) |
| `pressKey` extended | name table over existing keyevent path: volume up/down (24/25), home (3), lock (276), power (26), tab (61), escape (111) — deliberate deviation: Maestro maps Tab to 62, which is `KEYCODE_SPACE` in AOSP (apparent upstream bug); Autonom uses the correct `KEYCODE_TAB` = 61 | home/lock via existing buttons, enter/backspace via HID; volume/power/tab/escape refuse |
| `launchApp.permissions` | map Maestro permission names → existing `pm grant/revoke` substrate, applied pre-launch | map → existing `simctl privacy`; services simctl lacks (notifications, camera, bluetooth on this toolchain) refuse per-service — note the existing `PRIVACY_SERVICES` tuple (`ios_simctl.py`) currently lists `camera`/`userTracking` that this toolchain's simctl does not advertise, so the tuple is adjusted rather than reused as-is |
| `openLink.browser` | retarget VIEW intent to a browser package with fallback chain | refuses (documented; Maestro silently ignores it) |
| `setDarkMode`/`toggleDarkMode` (stretch) | `cmd uimode night yes\|no` | `simctl ui <udid> appearance dark\|light` |

`openLink.autoVerify` stays refused with a pointed hint (OEM-varying dialog automation — heuristic UI driving, out of scope).

**Spike (before this slice): `docs/plans/spikes/0.28.2-android-airplane-cmd.md`** — on-device check that `cmd connectivity airplane-mode` is functional on target API levels (Maestro keeps a fallback because some builds answer "No shell command implementation."), and whether `screenrecord --time-limit 0` (API≥34) should be added to lift the 3-minute recording cap. Verdict format per existing spikes.

## 4. Slice 0.28.3 — launch arguments, suite config, closing

### 4.1 `launchApp.arguments`

iOS: existing `ios_simctl.launch` already accepts args and `SIMCTL_CHILD_*` env — plumb through the flow executor. Android: requires replacing the `monkey`-based launch with `cmd package resolve-activity --brief` + `am start -n <component>` carrying typed extras (`--es/--ez/--ei/--ef/--el`), used only when arguments are present.

**Spike (gates this slice): `docs/plans/spikes/0.28.3-launch-extras.md`** — resolve-activity coverage and extras delivery on target API levels; if <reliable, `arguments` ships iOS-only and Android refuses with a hint until the follow-up.

### 4.2 Workspace suite config

`flow run <dir>` already filters by `--include-tag`/`--exclude-tag`, continues past test failures, and aggregates results; `flow report` already emits JUnit. The gap is a config file: `.autonom/config.yaml` (strict-subset parsed — no new YAML features) with `flows:` (glob list), `includeTags`/`excludeTags`, `executionOrder: {flowsOrder, continueOnFailure}`. CLI: `flow run <dir> [--config PATH]`. Cloud-only Maestro keys refuse by name with a hint. Sequential execution only.

### 4.3 Closing

- `docs/FLOW.md` `## Maestro Core Profile` rewritten as the v2 contract, with a per-command compatibility table (supported / argument caveats / refused-with-hint) — this table is the public promise.
- Export parity sweep: every newly native command gains an export mapping where Maestro can express it (`copyTextFrom` without `into`, `repeat` with literal `times`, `scroll`, `killApp`, `hideKeyboard`, airplane/keychain/travel/recording, inline `runFlow`); `into:`-variable flows refuse on export (Maestro's equivalent is JS — see open decision D-o4).
- Adversarial review pass in the 0.27.3 tradition; CHANGELOG `### Fixed` closing release if needed.

---

## 5. Error vocabulary

Prefer existing codes. Add-only candidates (each needs a `_FAILURE_CLASS_BY_CODE` entry and a `docs/FLOW.md` `## Errors` mention):

- `flow_repeat_invalid` (static, `flow_definition`) — `times` missing/out of bounds, nested repeat.
- `flow_var_conflict` (static, `flow_definition`) — `into:` name collides with a declared env/secret name.
- Reuse `unsupported_on_platform`, `unsupported_flow_command`, `flow_selector_invalid`, recording-active and location errors as-is. `flow_not_found` remains network-capture's forever.

## 6. Testing strategy

| Layer | What |
|---|---|
| Parser | corpus rows for single-line flow mappings (accept in import mode, refuse natively — both directions pinned); alias table unit tests |
| Schema/docs gate | `tests/test_docs_flow_surface.py` — fence updated per slice: new commands/args; `repeat` leaves `deferred:` (`DEFERRED_COMMANDS`); `containsDescendants` leaves `SELECTOR_DEFERRED_FIELDS` and joins the `selector-relational` fence list |
| Import | new fixture corpus under `tests/fixtures/maestro/` including flow-mapping files, hooks, clipboard trio, repeat; refusal-position tests for everything in §0.1; round-trip self-check stays mandatory |
| Executor | fake-driver semantics tests: variable frame precedence + sensitivity propagation, repeat iteration events, killApp/hideKeyboard dispatch, platform refusals per §3 table, recording double-start |
| Export | round-trips for every newly exportable command; `into:` refusal |
| CLI | `flow run` Maestro auto-detect end-to-end (convert → pre-flight → run under fake adb); `--config` suite selection; `tests/test_bare_host.py` SWEEP rows for any new flag; `tests/test_docs_cli_surface.py` + the `docs/CAPABILITIES.md` CLI-surface fence for `--config` and any `flow run|check|fmt` option changes |
| Hygiene | env-isolation for every new test touching `AUTONOM_HOME`; scratch-HOME sweep; contract golden untouched (flow-run JSON changes are additive only) |

## 7. Security

- Copied variables can hold credentials read off-screen: values from sensitive-marked sources keep the `sensitive` flag; `pasteText` of a sensitive value redacts like `inputText.sensitive`; variables never land in the manifest or reproduction command.
- Auto-running third-party Maestro files: the JS pre-scan refusal, workspace path containment for `runFlow`, and full pre-flight all run before any device effect — an imported file gets no capability a native flow lacks.
- `repeat` is bounded (≤25) — no unbounded loops enter the language.
- Recording artifacts inherit session file permissions; launch extras may carry secrets → documented interaction with `--secret` (extras echoing is redacted in events).

## 8. Decision log

| # | Decision |
|---|---|
| D1 | Flow mappings parse in import mode only; the native grammar stays strict |
| D2 | Clipboard trio is a host-side register; the OS clipboard is never touched (matches Maestro's actual behavior) |
| D3 | `repeat` requires a literal bound (`times` 1–25); `while` is early-exit only; no `allowMutations` gate — declared iteration is not failure recovery |
| D4 | Platform gaps refuse with `unsupported_on_platform`; Maestro-style silent no-ops are never reproduced |
| D5 | `killApp` on iOS is a documented terminate alias |
| D6 | `flow run` auto-detects Maestro files by the absent `schema:` header field and converts in memory; the conversion is journaled |
| D7 | Import aliases never enter canonical Flow v1; `flow fmt` output stays single-spelling |
| D8 | The v2 exclusion list (§0.1) is part of the documented profile contract in `docs/FLOW.md`, each item with its refusal hint |

## 9. Open decisions (resolve during the slices)

1. (0.28.1) `containsDescendants` depth limit and event reporting for the walk.
2. (0.28.1) `traits` / `width`/`height`/`tolerance` selectors: current position is refuse-with-hint (heuristic matching conflicts with exact-match philosophy) — revisit only on real demand. **D-o2**
3. (0.28.2) `travel` on iOS: engine loop vs delegating to `simctl location start --speed` (fidelity vs one less moving part). **D-o3**
4. (0.28.3) Export of `into:`-variable flows: refuse (current position) vs emitting Maestro `copyTextFrom` + `${maestro.copiedText}` for the single-variable case. **D-o4**
5. (0.28.3) Suite config file location precedence when both `.autonom/config.yaml` and `--config` exist.
6. (0.28.0) Whether `flow fmt` on a Maestro file writes the converted Flow v1 (probably yes — it is the migration tool).

## 10. Related documents

- `docs/plans/PHASE_5_FLOW_DSL.md` — the language this phase extends
- `docs/AUTONOM_PRODUCT_RESEARCH_REVYL_MAESTRO.md` §15 — Core Profile commitment
- `docs/FLOW.md` — language surface fence + Maestro Core Profile section (both updated per slice)
- `docs/COMPATIBILITY.md` — error-code and exit-code commitments
- `docs/plans/spikes/0.28.2-android-airplane-cmd.md`, `docs/plans/spikes/0.28.3-launch-extras.md` — scheduled spikes
