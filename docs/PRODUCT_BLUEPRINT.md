# Autonom product architecture

This document is the implementation contract for the Master Implementation
Blueprint, the Mobile Canvas adoption plan, and the Allure Report research.
The code is split into a capture plane and a derived reporting plane so new
exporters never change what a device run records.

## Contract stack

| Contract | Identifier | Owner |
| --- | --- | --- |
| Flow | `autonom.dev/flow/v1` | strict authored intent |
| Event | `autonom.event/v1` | ordered runtime facts |
| Provider | `autonom.provider/v1` | immutable capability snapshot |
| Legacy capture manifest | numeric v3 | backward-compatible executor ledger |
| Report Model | `autonom.report/v2` | exporter-neutral domain model |
| Replay manifest | `autonom.replay/v1` | pinned reconstruction inputs |
| Report Bundle | `autonom.bundle/v2` | portable finalized evidence |

Machine-readable JSON Schemas live in `schemas/`. The Python validators are
dependency-free and run on every compile/export path.

## Identity and status

A test case has a stable `case_id` from app plus flow identity. Its
`history_id` additionally includes only explicitly declared, low-risk business
parameters. A run always gets a fresh `attempt_id`; replay and retry record the
parent attempt instead of overwriting it.

Status has two independent axes:

- execution: `passed`, `failed`, `broken`, `skipped`, or `unknown`;
- proof: `pass`, `fail`, `not_covered`, `blocked`, `inconclusive`, or
  `not_applicable`.

Test failures and infrastructure/definition failures therefore remain
distinguishable. History keeps every attempt, uses the latest attempt as the
current outcome, and marks recovered/flaky cases without deleting earlier
evidence.

## Flow setup and provider preflight

Flow v1 accepts `requires`, `sideEffects`, and `setup`. `requires` uses a
frozen semantic capability vocabulary rather than tool names. Before the
first mutation the executor captures an immutable provider snapshot and
checks explicit plus setup-inferred requirements.

The Setup Catalog records five separate collections: available, selected,
applied, verified, and used. Provider-owned setup such as location,
permissions, reset, orientation, appearance, and network state is applied
before flow hooks. App profile, fixture, mock, and locale selections remain
explicitly external until their owning runner resolves them; they are never
reported as applied merely because they were declared.

`autonom capabilities` exposes the exact snapshot used for planning. A
physical device is never silently treated as a simulator.

## Teach and App Skills

Teach implements the full recording lifecycle:

1. `teach start` creates a range in the append-only journal.
2. `teach mark` creates named subrange boundaries.
3. `teach stop` freezes the raw range.
4. `teach compile` derives semantic selectors, secrets, confidence, and
   provenance, then parses the result back through the strict Flow compiler.
5. A human reviews and runs the candidate.
6. `teach approve` requires three consecutive clean replays by default; it counts
   past runs, or performs them itself with `--run`.

Approved knowledge can be promoted to
`.autonom/apps/<app-id>/` with `app-skill promote`. The fixed portable layout
is `SKILL.md`, `selectors.yaml`, `subflows/`, `fixtures.yaml`,
`compatibility.yaml`, and `catalog.json`. Validation rejects malformed flows,
missing required files, unsafe app identifiers, and credential-shaped prose.
No arbitrary helper program is auto-loaded from an App Skill.

## Evidence and Delta

Every event carries ordered sequence, monotonic and UTC clocks, source,
attempt/step attribution, and a pre-persistence redaction receipt. Each step
records the selector receipt, matched target, before/after fingerprints, exact
artifact ownership, and all captured log/network ranges.

The model keeps authored actions and their runtime `ActionAttempt` records as
separate entities. Each action attempt has its own stable identity, retry
number, timing, status, error, and attachment links while still referencing
the parent test attempt and authored step.

Report Model v2 materializes a complete Delta per step:

- before/after screenshots and UI trees;
- UI hierarchy change data;
- all attributed log attachments;
- all attributed requests;
- selector and target receipts;
- precondition and postcondition fingerprints;
- an explicit `zero` versus `unavailable` state.

The report UI provides addressable step deep links, a filterable keyboard
navigator, first-causal-failure link, screenshots, matched bounds, hierarchy
diff, logs, requests, setup/capability inspector, and replay-to-step controls.
The static report works offline; detailed suites use a multi-file layout so
large evidence is not inlined into the index.

## Report Bundle v2

`report bundle` writes through a sibling staging directory and atomically
finalizes:

```text
manifest.json
run.json
model/report.json
summary.json
catalog.json
replay.json
steps/<step-id>.json
streams/events.ndjson
flow/.autonom/
flow/<portable source graph>
blobs/sha256/<prefix>/<digest>
integrity.json
finalized.json
annotations/*.json
```

Artifacts are content-addressed and deduplicated. `report verify` checks every
recorded digest. A finalized bundle is immutable and a repeated finalize of
the same run is idempotent. Human annotations live beside raw evidence and do
not alter its integrity set. Flow source graphs are copied into the bundle so
baseline replay does not depend on the original absolute checkout path.

## Exporters and gates

The native model fans out to interactive HTML, JUnit XML, Allure results,
compact agent JSON, per-step CSV, metrics JSON, and the native bundle. Stable
case/history identity makes Allure retries merge instead of appearing as new
tests. Allure is an exporter, not the internal data model.

Gates consume explicit JSON rules and return typed gate results. Execution
outcome and publication outcome are independent: an upload failure never
rewrites a passing device run into a test failure.

## Replay and checkpoints

`autonom replay` accepts a live run or a portable bundle. Baseline replay is
the default and never claims to be a native snapshot. A selected runtime
index, stable step id, or checkpoint name pins the expected identity and
status. Each replay creates a child attempt, verifies intermediate steps,
captures full evidence, stops after the selected step, and skips cleanup so
the device remains inspectable. Missing source, secrets, capabilities, build
inputs, or a divergent step produce typed blockers.

Provider checkpoints advertise whether they are native or portable-prefix.
Physical targets explicitly refuse snapshot creation. The portable strategy
replays from the flow baseline and is available across providers.

## Supervised CI and aggregation

`autonom ci run` owns the complete local lifecycle:

```text
capture -> spool -> bundle -> pack -> merge -> gate -> finalize -> publish
```

State is written after every phase to `ci-state.json`. Shard packs are regular
ZIP files containing a verified bundle and a shard descriptor. Merge is
idempotent by attempt identity, and expected versus received shard counts make
missing work visible. Publication supports an empty filesystem destination or
an HTTP PUT endpoint with an idempotency digest. Standalone `ci pack`, `merge`,
`finalize`, and `publish` commands support distributed runners.

## Mobile Canvas

Canvas is a view and control surface, not a second device automation plane.
All tap, swipe, key, and text actions travel through one persistent Python
NDJSON bridge, use the same platform-neutral action functions as the CLI, and
land in the same action detail store and journal. Origins are human, agent,
replay, or system. The control endpoint supports pause, resume, takeover, and
release, and disconnecting the browser does not stop the device session.

Security defaults are loopback binding and a fragment bootstrap token. The
fragment is exchanged once for an HttpOnly, SameSite cookie plus CSRF token and
then removed from browser history. Legacy bearer/query authentication remains
only for authenticated non-browser clients. Android exposes direct Annex-B
H.264 and keeps accelerated MJPEG and screenshot polling fallbacks. iOS uses
public `simctl` PNG capture plus idb input; logical point coordinates are
measured separately from Retina pixels.

`autonom canvas serve` selects an explicit Android target or booted iOS
Simulator and starts the shared surface.

## Simulator controls and Runtime Map

`autonom simulator` exposes battery, network, push, SMS, call, biometric,
clipboard, appearance, text size, and status-bar controls. Unsupported
platform combinations return `unsupported_capability`; no command reports a
successful no-op. Every successful response includes a verification receipt.

The existing observed application graph is the Runtime Map. `runtime-map` is
an alias for `atlas`, including update, show, coverage, paths, export, and
diff. It ingests the same event and action evidence rather than maintaining a
second graph.

## Security and validation

Redaction runs before persistence and again before derived bundle creation.
Credential-shaped values, authorization headers, cookies, and typed sensitive
text are not copied into a report model. Bundle files use owner-only modes.
Loopback services have bounded request bodies, no-store responses, explicit
authentication, and path-containment checks. Finalized evidence and mutable
annotations are separated.

Validation climbs from schemas and Flow round trips through executor/exporter
tests, fake-device CLI end-to-end tests, Canvas HTTP/auth/input tests, full
`make check`, and finally explicit-target device replay when hardware is
available. The repository remains Python standard-library plus Node.js with no
package manager or service dependency.
