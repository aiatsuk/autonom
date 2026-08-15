# Phase 0: reliable foundation — test isolation, CI, tagged releases

**Status:** implemented; ships as 0.15.2.
**Goal:** make "green" trustworthy before any new subsystem lands — the suite
must not touch the operator's machine state, checks must run on infrastructure
nobody's laptop mood controls, and a release must be reproducible from a tag.
**Depends on:** the 0.15.1 baseline (all shipped phases).
**Target versions:** | Version | Theme |
| --- | --- |
| 0.15.2 | Test isolation + CI + release engineering (this document) |

---

## 0. Why this phase exists

Two suites deleted ambient `AUTONOM_HOME` from `os.environ` instead of
restoring it (`tests/test_devices_lifecycle.py`, `tests/test_network.py`), one
test popped `CI` — reachable the moment GitHub Actions (which sets `CI=true`)
exists — and six call sites reached the operator's real `~/.autonom` because
the session store mkdirs at call time. Meanwhile the repository shipped no CI
configuration and zero git tags: "green" meant a clean local run on one
machine, and a release was an untagged, unchecksummed tarball built from
whatever the working tree happened to contain.

### 0.1 Non-goals (explicit)

- No CLI verbs, flags, or response keys change in this phase.
- No iOS-simulator smoke job (deferred with the provider work).
- No PR-triggered emulator smoke — main-branch + manual dispatch only;
  revisit only if a main-branch failure would have been caught earlier on a PR.
- No production `autonom_lib` changes.

### 0.2 Design principles

- Per-test isolation is the fix; the `run_checks.sh` belt is defence in depth,
  never a substitute.
- CI runs the same script contributors run — no forked check logic.
- A refusal is not a timeout: the smoke's polling loop aborts immediately on
  `ambiguous_selector` instead of burning its budget on a permanent condition.

## 1. What shipped

| Area | Deliverable |
| --- | --- |
| Isolation | `tests/env_isolation.py` (`EnvSandboxMixin.set_env`/`sandbox_home`, addCleanup-based restore); three buggy mutators converted; six real-home holes closed (`test_autonom_cli`, `test_doctor` ×2, `test_ios_ui` ×2, `test_platform_targets` ×2) |
| Guard | `tests/test_aa_env_snapshot.py` (alphabetically first, snapshots `os.environ` at import) + `tests/test_zz_env_hygiene.py` (last; compares sentinels `AUTONOM_*`, `CI`, `HOME`, `XDG_STATE_HOME`; also pins all four machine-store resolvers under a redirected root and the documented fallback chains) |
| Belt | `run_checks.sh` exports a scratch `AUTONOM_HOME` with trap cleanup; prunes `dist/` from shell sweeps; `AUTONOM_REQUIRE_SHELLCHECK=1` makes a missing shellcheck fatal |
| CI | `.github/workflows/checks.yml` — actionlint job + full `run_checks.sh` on ubuntu (py 3.11, 3.14) and macOS (py 3.14), Node 22, shellcheck/openssl gated |
| Smoke | `.github/workflows/android-smoke.yml` + `scripts/ci/android_smoke.sh` — real API-30 x86_64 emulator (KVM, AVD snapshot cache), preinstalled Settings app, devices → session → launch → read-only poll (ambiguity fast-abort) → tree → exact tap (once, no retry) → screenshot → stop, artifact assertions, session dir uploaded on failure |
| Release | `CHANGELOG.md`; `build_release.sh` hardened (library-import version resolver, loud pre-flight for required files, checks-before-staging, `SHA256SUMS`); `.github/workflows/release.yml` gated on tag↔`__version__` agreement; `docs/COMPATIBILITY.md` |
| Docs | `ARCHITECTURE.md` CI claims corrected; `CONTRIBUTING.md` release rule; `INSTALL.md` minimum versions; README doc index |

## 2. What the smoke job proves — and does not

Proves: the CLI's device-discovery → session → tree → exact-find → tap →
screenshot → teardown pipeline works against a real UI Automator/adb backend,
and artifacts land where the docs say.

Does **not** prove: anything iOS; installing or launching a third-party APK;
the network/MITM path or consent gating (consent is untestable
non-interactively *by design*); selector behavior under real duplicates;
other API levels or locales; physical devices; performance.

## 3. Exit criteria

- [x] Suite run twice with ambient `AUTONOM_HOME` exported leaves `os.environ`
      and the real `~/.autonom` / `~/.local/state/autonom` untouched.
- [x] Guard proven live: fails against the pre-fix `tearDown`, passes after.
- [x] The `CI` env var survives the in-process suite.
- [x] Fallback chains (`~/.autonom`, `$XDG_STATE_HOME/autonom`,
      `~/.local/state/autonom`) keep test coverage despite the belt.
- [x] `build_release.sh` with `LICENSE` deleted fails at pre-flight before
      staging; version parsing no longer greps.
- [x] `run_checks.sh` green locally (shellcheck installed and running).
- [ ] `checks` green on all three matrix jobs (first push).
- [ ] `android-smoke` green on main (first push).
- [ ] `git tag v0.15.2 && git push --tags` produces a GitHub Release with
      `autonom-0.15.2.tgz` + `SHA256SUMS`; a mismatched tag fails before build.

## 4. Risks

| Risk | Mitigation |
| --- | --- |
| First-ever Linux run surfaces a darwin-ism | No `sys.platform` guards exist anywhere in `scripts/` or `tests/`; treat the first ubuntu run as discovery and fix in tests, never by skipping |
| Emulator smoke flake erodes trust | main-only trigger, pinned API 30, AVD snapshot cache, animations off, read-only polling with ambiguity fast-abort, assertions limited to `ok` + artifact existence, session upload on failure |
| Double suite run (~56 contract-probe subprocesses per `run_checks.sh`) exceeds CI patience | `timeout-minutes: 20`; fallback: split the tty-guard re-run into its own step |
| Belt masks a future per-test isolation regression | The guard compares in-process snapshots, which shell-level redirection cannot satisfy |

## 5. Decision log

| # | Decision |
| --- | --- |
| DEC-014 | Real CI supersedes DEC-012 ("the bare-host sweep replaces CI"). The sweep is retained as the empty-PATH invariant (VER-011/INV-08), no longer as the CI substitute. `tests/test_bare_host.py` docstring updated. |
| DEC-015 | Version lanes: 0.15.2 = this phase; 0.16–0.19 remain reserved for the metrics work documented in `PHASE_4_METRICS.md`; the Flow DSL takes 0.20.x as house **Phase 5** (`docs/plans/PHASE_5_FLOW_DSL.md`). This renumbers `PHASE_2_3_IOS_NETWORK.md`'s D10, which had penciled the MCP wrapper in as "Phase 5" — the MCP wrapper moves after the Flow DSL. |
| DEC-016 | `AUTONOM_REQUIRE_SHELLCHECK` is dev tooling, not CLI surface: documented in the `CAPABILITIES.md` environment table, deliberately outside the machine-checked CLI-surface fence. |
| DEC-017 | The smoke targets the preinstalled Settings app — no APK to build, install, or pin; third-party-app smoke waits for the Flow DSL slice that can express it as a flow file. |

## 6. Related documents

- `docs/COMPATIBILITY.md` — the frozen CLI contract this phase wrote down.
- `docs/plans/PHASE_5_FLOW_DSL.md` — the next phase, which depends on this one.
- `SECURITY.md` — unchanged; the belt and guards defend its "local state only"
  invariant against test bugs.
