# Phase 7: Ticket pre-test — local run, evidence, and QA pack

**Status:** implemented (slices A–E in this change, unreleased).
**Goal:** turn "one ticket plus its diff" into a reviewable pre-test pack made
on a local Android emulator or iOS Simulator: what was checked, how, what was
found, and the evidence for each claim. A QA engineer reviews the pack instead
of re-testing the ticket from scratch.
**Based on:** the 2026-10-10 market research (the unit of value is a
pre-tested ticket with an evidence pack, not another natural-language test
runner) and the current code at 0.35.1.
**Target versions:** 0.36.0–0.40.0, one slice per minor version.

---

## 0. Scope

### 0.1 In scope

- A local pre-test run on one explicit target: the ticket and the diff go in,
  checks are planned and run, and evidence is captured and tied to each check.
- Collecting and preparing that evidence: device health before and after,
  findings, steps to reproduce, draft regression flows, and time spent.
- A portable, integrity-checked pack plus a Markdown summary that a person
  can paste into a ticket or PR by hand.
- A local QA review loop: approve, ask for more checks, or return to the
  developer.

### 0.2 Out of scope (later, as separate phases)

- Any network deployment: hosted service, multi-tenant storage, accounts,
  billing, usage metering, EU hosting, SSO, SOC 2.
- Integrations: GitHub or GitLab PR comments, Jira, Linear, YouTrack, the MCP
  server, Slack, and HTTP publishing of packs (`ci publish` exists but this
  phase does not use it).
- Device farms and physical devices beyond what already works.
- Code fixes made by Autonom. The coding agent fixes the code. Autonom only
  re-checks the fix and records before and after (§7).
- Any LLM call inside Autonom. The thinking stays in the agent; Autonom gives
  structure, limits, capture, and packaging. This matches Flow v1's rule of no
  model-based grounding in the executor.

### 0.3 Design principles

1. **The agent decides, Autonom records.** Autonom never guesses what a ticket
   means. The agent reads the ticket and writes the checks, and Autonom makes
   every claim traceable to evidence.
2. **Honest verdicts.** The existing vocabulary (`pass`, `fail`,
   `not_covered`, `blocked`, `inconclusive`) is reused per check and for the
   whole ticket, and is never upgraded. A ticket with an unrun check is not a
   `pass`.
3. **Capture first, render later.** New data goes into append-only records
   first. The Markdown and HTML views are derived from them, as with the
   Report Model.
4. **Reuse, don't fork.** The journal, the `--task` label, `proof`
   selection, Bundle v2 blobs and integrity, `report annotate`, the Session→Flow
   compiler, the Runtime Map, and the network/crash/log/metrics verbs already
   exist. The new code mostly connects them.
5. **Safety limits are enforced, not advised.** Budgets and forbidden actions
   are refused by the CLI while a pre-test is active.

## 1. What exists today, and the gaps

| Need | Exists | Gap |
| --- | --- | --- |
| Diff → what to test | `proof.changed_files`, `select_suite` (changed flows, `runFlow` reach, `covers` globs, the `pull-request` tag) | Reads file names only, not hunks. No ticket text. No link to Runtime Map screens. Output is not a bundle. |
| Grouping ad-hoc work | `--task` on `screenshot`, `note`, `journal`, `flow create` | Not applied to trees, actions, network, or logs. No start or end of a task. |
| Evidence storage | session dirs `shots/`, `trees/`, `actions/`, `network/flows.jsonl`, `logs/`, `crashes/`, `metrics/`, `recordings/` | No model or bundle for non-flow (exploratory) work. Bundle v2 is tied to one flow run. |
| Portable proof | Bundle v2: content-addressed blobs, `integrity.json`, `finalized.json`, `report verify` | The writer is specific to flow runs. |
| Review | `report annotate` (outside the integrity set) | No review decision, no "more checks" loop. |
| Findings | none (only flow pass/fail; `gates.history` marks flaky) | No finding record, severity, or repro steps. |
| Device health | `crash list`, `logs tail`, `network requests list`, `metrics snapshot` | No "new since the start" comparison and nothing feeds a report. |
| Regression from exploration | `flow create --from-session`, Teach compile and approve | Not tied to a finding. |
| Human QA in Canvas | Canvas Capture tab, Tools drawer | Captures go to `~/Downloads/Autonom/…`, outside the session and without `mocks_active` provenance. The activity log is memory-only. |
| Per-app knowledge | `~/.autonom/apps/<id>/` (Runtime Map, mobile-memory notes); workspace `.autonom/apps/<id>/` (App Skills) | Two roots, no read order, no test-account or login profile. |

## 2. Shape of a pre-test

```text
pretest start (ticket + diff + target)
  → baseline: crash watermark, log cursor, network cursor, metrics snapshot
  → context: changed files and hunks, selected flows, Runtime Map screens
  → agent plans checks (ticket, edge cases, regression)
  → for each check: begin → drive device / run flows / set mocks → end with verdict
  → findings: written by the agent + automatic ones from device health
  → pretest pack: integrity-checked bundle + summary.md + summary.json + offline HTML
  → QA review: approve | more-checks (child pre-test) | return-to-dev
  → after a fix: rerun the affected checks → before/after in a new pack
```

One pre-test lives inside one session and has its own id. Data is stored
under `~/.autonom/sessions/<sid>/pretests/<pretest-id>/`:

```text
pretest.json        record: ticket ref, base/head, target, budget, status, timings
ticket.md           ticket text, redacted before it is written
context.json        changed files, hunk summaries, selected flows, Runtime Map hits
baseline.json       crash/log/network cursors and metrics at start
checks.ndjson       append-only: added, begun, ended (verdict, note)
findings.ndjson     append-only: manual and automatic findings
health.json         deltas at pack time: new crashes, error logs, failed requests, metrics
```

## 3. Slice A (0.36.0) — the pre-test record, attribution, and limits

**Size:** M.

Commands:

```bash
autonom pretest start --ticket FILE|- --base REF [--head REF] [--repo PATH] \
    [--flows DIR] [--title TEXT] [--ticket-ref KEY] \
    [--max-actions N] [--max-minutes N] [--forbid-text PATTERN]... [--allow-app ID]...
autonom pretest status            # active pre-test, budget used, open check
autonom pretest context           # changed files, hunks, flows, screens (JSON)
autonom pretest stop              # closes the pre-test without a pack
```

Work items:

1. **Record and lifecycle** — a new module `scripts/autonom_lib/pretest.py`.
   One active pre-test per session (`pretest_already_active`). A second
   `start` is refused, exactly like `session start`.
2. **Diff context** — extend `proof.py` with a hunk reader
   (`git diff -U0 base[...head]`): per file, the changed line ranges and the
   names of changed symbols where the hunk header gives them. Cap the size
   (`diff_truncated: true` past the cap). Reuse `select_suite` for flows. Add
   Runtime Map hits: screens whose recorded evidence or labels match changed
   areas. These are hints only (`hint: true`), never coverage.
3. **Attribution** — when a check is open (Slice B), every journaled verb gets
   `pretest_id` and `check_id`, and the `--task` label is set automatically for
   screenshots, trees, action details, and notes. Network flows and log lines
   are attributed by time window (begin/end timestamps of the check), which the
   capture layer already records.
4. **Baseline** — at `start`, record the crash-store watermark (newest crash
   id and time), the log cursor, the `network/flows.jsonl` line count, and one
   `metrics snapshot`. Missing capabilities are written as `unavailable` with
   the reason, never as zero.
5. **Budgets and safety** — while a pre-test is active:
   - `ui tap`/`type`/`swipe`, `open`, `app launch`, and flow steps count
     against `--max-actions`; past the limit they are refused with
     `budget_exhausted`;
   - `--max-minutes` is checked the same way;
   - a tap whose resolved node text or label matches `--forbid-text` (for
     example `Buy|Delete|Pay`) is refused with `forbidden_action` and
     journaled;
   - an app id outside `--allow-app` is refused for `launch`/`open`.
   Defaults are conservative and printed in `pretest status`.
6. **Schema** — `schemas/pretest-v1.schema.json` for `pretest.json`, plus a
   round-trip test that every field `status` and `pack` read survives a
   write/read.

Tests: fake-device CLI end to end (`tests/fakes/`): start → status → refused
second start → budget refusal → forbidden-text refusal → stop. Diff-hunk
reader on a temp git repo, including renames, binary files, and an empty diff.

## 4. Slice B (0.37.0) — checks, findings, device health, and the pack

**Size:** M.

Commands:

```bash
autonom pretest check add "<title>" --kind ticket|edge|regression|network|a11y \
    [--why TEXT] [--flow PATH]          # -> check_id
autonom pretest check begin <check_id>
autonom pretest check end <check_id> --verdict pass|fail|blocked|not_covered|inconclusive \
    [--note TEXT]
autonom pretest check list
autonom pretest flows [--env K=V] [--secret NAME]   # runs the selected flows as checks
autonom pretest finding add --check <id> --severity blocker|major|minor|info \
    --title TEXT --expected TEXT --actual TEXT [--evidence REF]...
autonom pretest health            # deltas since baseline, any time
autonom pretest pack [--out DIR]  # finalizes and writes the pack
```

Work items:

1. **Checks** — `checks.ndjson` is append-only. Only one check is open at a
   time. A check never begun at pack time is `not_covered`. A check whose flow
   run broke on infrastructure is `blocked`.
2. **Flows as checks** — `pretest flows` runs the `proof` selection through
   the existing `Executor`. Each run becomes a `regression` check with its
   `run_id` and verdict, so `proof`'s logic is reused, not copied.
3. **Findings** — written by the agent, each linked to a check and to
   evidence refs (screenshot ids, tree files, action ids, request ids, log
   ranges). Refs are validated at write time (`evidence_ref_unknown`).
4. **Automatic findings from device health**, at pack time and by
   `pretest health`:
   - new crashes since the watermark (Android crash buffer, iOS crash store),
     tagged `auto: true, source: crash`;
   - new error-level log lines from the app, grouped by message with the
     dynamic parts removed (counts, not full dumps);
   - captured requests with 5xx, or 4xx not set up by an active mock, and
     requests that never finished;
   - a metrics lead when the end snapshot differs from the start past a
     threshold. These are always called "leads", never leaks, following the
     existing metrics wording.
   Each automatic finding is `severity: info` until the agent or a reviewer
   confirms it. Automatic findings never change a check's verdict by
   themselves.
5. **Steps to reproduce** — derived from the journal range of the finding's
   check: the verbs in order, with selectors, mocks active, and simulator
   state. Redacted with the existing scrubbers.
6. **The pack** — refactor the generic part of `report_bundle.build()`
   (staging dir, content-addressed blobs, `integrity.json`, atomic finalize,
   idempotent re-finalize) into a shared writer. The flow-run bundle and the
   pre-test pack both use it. Pack layout:

   ```text
   manifest.json            kind: autonom.pretest-pack/v1
   pretest.json  ticket.md  context.json  baseline.json  health.json
   checks/<check-id>.json   verdict, timings, evidence refs, repro steps
   findings/<id>.json
   runs/<run-id>/           nested Bundle v2 of each flow run
   summary.json             compact agent view
   summary.md               paste-ready text for a ticket or PR
   index.html               offline viewer: checks, findings, evidence
   blobs/sha256/…  integrity.json  finalized.json  annotations/
   ```

   `report verify` accepts a pre-test pack, and nested run bundles are checked
   too.
7. **Ticket verdict** — `fail` if any check failed or any blocker or major
   finding is confirmed; `blocked` if a check is blocked and nothing failed;
   `not_covered` if a planned check never ran; `inconclusive` if evidence is
   missing; `pass` only when every planned check passed. The summary always
   lists what was not covered.
8. **Time spent** — wall time per check and per pre-test, number of actions,
   number of checks and findings. This is the raw data for the "hours saved"
   numbers later. No money figures in this phase.
9. **Schemas** — `pretest-check-v1`, `pretest-finding-v1`, and
   `pretest-pack-v1` JSON schemas, with round-trip tests from record to pack to
   `summary.json`.

Tests: one fake end to end that produces a pack with two checks (one pass, one
fail with a finding), a fake crash between baseline and pack, and a mocked 500.
Then `report verify` passes, a changed blob is detected, re-pack is
idempotent, and `summary.md` lists the uncovered check. Edge cases: a pack with
no checks (`not_covered`), a check left open, missing metrics
(`unavailable`), and a finding pointing at a deleted screenshot.

## 5. Slice C (0.38.0) — agent playbook, edge-case kit, Canvas, and QA review

**Size:** M.

1. **New skill `ticket-pretest`** (`plugins/autonom/skills/ticket-pretest/`):
   the agent's playbook.
   - Read the ticket and `pretest context`.
   - Write the checks before touching the device: ticket acceptance
     points, then edge cases from the kit, then regression flows.
   - Begin and end each check, and write findings with expected vs actual.
   - Separate measured facts from guesses.
   - Pack, and report the ticket verdict plainly.
   - Register the skill in `autonom` (the map) and `project-router`, and pass
     `validate_plugin.py`.
2. **Edge-case kit** — a documented list that maps each common risk to
   existing verbs, so the agent does not improvise:

   | Risk | Verbs |
   | --- | --- |
   | Offline / slow / server error / timeout | `simulator network`, `network mock add` (status, delay) |
   | Background and resume, process death | `app` stop/launch, `simulator` controls |
   | Rotation, dark mode, large text, locale | `simulator appearance`, text size, `app.locale` |
   | Permission denied or revoked | `permissions` |
   | Empty, very long, emoji, RTL input | `ui type` |
   | Double tap, fast back | `ui tap`, `key back` |
   | Deep link into the changed screen | `open` |

   A check can name its kit item (`--kind edge --case offline`), so the
   summary shows which risks were covered and which were skipped.
3. **Canvas captures into the session** — when a session is live on the
   device, Canvas captures are also written to `shots/` with the same
   provenance as `autonom screenshot` (`mocks_active`, task, pre-test, and check
   ids). The Downloads copy stays for people. The Canvas activity log of device
   actions is already journaled; mark human actions with `origin: human`, so
   a QA engineer's manual checks in Canvas count as evidence for an open
   check.
4. **QA review**:

   ```bash
   autonom pretest review <pack> approve [--note TEXT]
   autonom pretest review <pack> more-checks --check "<title>"... [--note TEXT]
   autonom pretest review <pack> return-to-dev --finding <id>... [--note TEXT]
   ```

   A decision is an annotation (`autonom.pretest-review/v1`) beside the pack,
   outside its integrity set, like `report annotate`. `more-checks` starts a
   child pre-test that links the parent pack, already holds the requested
   checks, and keeps the parent's context. The child's pack lists both. A
   reviewer may also confirm or dismiss automatic findings
   (`--confirm`/`--dismiss <finding-id>`).
5. **Review view** — `index.html` shows the review state and the commands
   to copy. Buttons in a local served page (`report serve` style) come only if
   the copy-paste flow proves awkward in Slice C testing.

Tests: skill validation, a Canvas capture test that lands in `shots/` with
provenance, review annotations that leave `report verify` green, and a
child pre-test linked to its parent.

## 6. Slice D (0.39.0) — from findings to regression, flaky vs real

**Size:** M.

1. **Draft flow per finding** — `pretest flow-draft <finding-id>` compiles the
   check's journal range with the existing Session→Flow compiler into
   `.autonom/flows/drafts/<ticket>-<finding>.yaml`. It carries
   `properties.covers` from the diff files, so the next `proof` selects it.
   Drafts follow the Teach rule: three clean replays before approval.
2. **Reproducibility** — `pretest repro <finding-id> --times N` replays the
   draft flow N times and records `reproduced: k/N`. The result is written on
   the finding: `stable` (N/N), `intermittent` (some), or `not_reproduced`
   (0/N). It reuses the `gates.history` flaky logic. An intermittent failure
   stays a finding; it is never hidden as noise.
3. **Rerun after a fix** — `pretest rerun <pack> [--check ID]...` on a new
   build starts a child pre-test, runs the chosen checks' flows (drafts
   included), and puts the old and new evidence side by side in the pack
   (`before`/`after` per check). The code fix itself stays with the coding
   agent.

Tests: draft compile → replay on the fake device; `repro` with a fake that
fails 1 in 3; `rerun` that turns a failed check into a pass with both
evidence sets kept.

## 7. Slice E (0.40.0) — app onboarding and extra local checks

**Size:** M. Each item can ship on its own.

1. **App profile, one read order** — a fixed lookup: the workspace
   `.autonom/apps/<id>/` first, then `~/.autonom/apps/<id>/`, reported by
   `pretest context` so the agent sees which was used. The profile adds:
   - a login subflow;
   - test-account names (secrets only by env-var name, never values; the App
     Skill validator already rejects credential-shaped text);
   - fixture data;
   - known flaky screens.
2. **Warm-up** — `pretest warmup` runs the login subflow and a short Runtime
   Map walk of the screens named in the diff hints, so the first pre-test on
   a new app starts from a known state.
3. **Accessibility check kind** — from the UI tree, not screenshots:
   - interactive nodes with no label;
   - tap targets below platform minimums (48 dp Android, 44 pt iOS);
   - duplicate labels on one screen.
   Results are findings with node bounds as evidence. Contrast and screen
   reader order are out of scope here.
4. **Analytics event check kind** — the app profile can name expected events
   (a request-matching rule on the captured traffic). A check of this kind
   reports matched, missing, and unexpected events, with request ids as
   evidence. It needs network capture with consent, like every network
   feature.

Tests: profile lookup order, warm-up on the fake device, a11y rules on fixture
trees (`tests/fixtures/`), and event matching on a recorded `flows.jsonl`.

## 8. Cross-cutting work

- **Docs:** `CAPABILITIES.md`, `USAGE.md`, `ARCHITECTURE.md`, and `README.md`
  for every new verb. `test_docs_cli_surface.py` already makes this a check.
- **Error codes:** add the new codes to `errors.py` and document them
  (`pretest_already_active`, `no_active_pretest`, `no_open_check`,
  `budget_exhausted`, `forbidden_action`, `evidence_ref_unknown`,
  `pack_finalized`).
- **Error-path review:** before each slice's review, check every refusal
  branch and error payload against its schema, as the working rules require.
- **Live proof per slice** — fakes alone are not proof. One real run on an
  Android emulator and one on an iOS Simulator with the fixture app
  (`tests/live/`, new `pretest_live.py`): a two-check pre-test with a forced
  500 mock, a pack, `report verify`, and a review decision.
- **Naming:** use "Runtime Map" in all new text, not "Atlas" (the research
  found it is a competitor's brand). The `atlas` alias stays for
  compatibility.

## 9. Order and size

| Slice | Version | Size | Depends on | Delivers |
| --- | --- | --- | --- | --- |
| A | 0.36.0 | M | — | pre-test record, diff hunks, attribution, baseline, limits |
| B | 0.37.0 | M | A | checks, findings, health deltas, pack, ticket verdict |
| C | 0.38.0 | M | B | agent skill, edge-case kit, Canvas evidence, QA review |
| D | 0.39.0 | M | B | draft regression flows, flaky vs real, rerun after fix |
| E | 0.40.0 | M | A (C helps) | app profile, warm-up, a11y and analytics checks |

After B, the core loop works end to end: ticket in, evidence pack out. C makes
it usable by an agent without hand-holding and by a QA engineer. D and E can
run in either order.

## 10. Prepared for later phases (not built here)

The pack is the only thing integrations need. Later phases can read
`summary.md` and `summary.json` and post them:

- a PR or MR comment (GitHub, GitLab);
- a ticket comment (Jira, Linear);
- an MCP tool ("pre-test this ticket, return the pack").

The same pack can be uploaded to a hosted viewer. None of that changes the
capture, the checks, or the pack format, so this phase leaves no rework for
them.

## 11. Decisions taken in this plan

1. Autonom stays LLM-free. The agent does the reasoning; the CLI enforces
   structure and limits.
2. Code fixes are not part of Autonom. Only re-checking a fix is (Slice D).
3. A pre-test is a session-level object with its own pack. The flow-run
   Bundle v2 stays as is, and both share one writer.
4. Automatic findings are `info` until confirmed. They never flip a verdict
   alone.
5. Workspace app profile is read before the home-dir one.
