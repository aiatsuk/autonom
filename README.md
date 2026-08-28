# Autonom

**Universal mobile test and debug harness for AI coding agents.**

Autonom gives Codex, Claude, Grok, and other skill-compatible agents repeatable
workflows to **test and debug Android and iOS apps**: own a target session, see
what is on screen (compact accessibility tree + screenshot), tap/type/gesture by
semantics, pull logs and crash reports, drive device state, and climb an evidence
ladder through builds, UI checks, performance, memory, and release validation.

The same verbs work on an Android emulator and an iOS Simulator, and the compact
node schema is identical on both, so one skill body drives either platform.

Skills are portable `SKILL.md` packages. The **CLI control plane** — installed
as `autonom` — is the stable JSON API. Helpers stay dependency-light. No MCP
server is required (optional MCP wrapper is planned).

**Shipped:** Android emulator + iOS Simulator sessions, UI trees, semantic
find/tap/gestures, screenshots and recordings, logs and crashes, deep links,
permissions, location, media and container files, consent-gated HTTP(S) capture
and mocking, a per-session journal of every action, and **Flow v1** —
repeatable flow files with exact selectors, polling assertions, and failure
classes ([`docs/FLOW.md`](docs/FLOW.md)), plus Maestro import/export,
session→flow compilation (`flow create --from-session`), addressable per-step
evidence reports (HTML/JUnit) with loopback replay controls, an observed application atlas, local PR proof
(`autonom proof --base`), live session watch (`session outputs`,
`logs follow`, `network requests follow`), and an `autonom metrics` family
(memory, CPU, frames, traces). Domain packs for Flutter, native
Kotlin/Jetpack Compose, and iOS.  
**Roadmap:** Flutter VM Service, React Native skills, optional MCP wrapper,
hosted device providers.

## Why Autonom

- **Agent-portable** — one skill pack for Codex, Claude, Grok, or any skill host.
- **CLI + skills** — agents shell out to one JSON CLI; not locked to a single MCP.
- **Accessibility-first** — compact UI trees for structure; screenshots for proof.
- **Routing first** — `project-router` loads the smallest relevant skill set.
- **Evidence second** — code → tests → device flow → tree/shot/logs → before/after.
- **Runtime-honest tooling** — inspect local SDKs; never hard-code “latest”.
- **Safe device bridge** — token-protected localhost emulator browser (Android).

## Control plane

Installed as `autonom` (or run `python3 scripts/autonom.py …` from a checkout
without installing):

```bash
autonom doctor            # what can this machine actually do?
autonom devices           # Android + iOS in one list (each has a `running` flag)
autonom devices boot --avd Pixel_9          # start an emulator and wait for boot
autonom devices shutdown --serial emulator-5554

# Android
autonom session start --serial emulator-5554 --app-id com.example.app

# iOS Simulator (boots it if needed)
autonom session start --platform ios --target <UDID> \
  --install build/ios/iphonesimulator/Runner.app --launch --app-id com.example.app

# Same verbs on either platform
autonom ui tree
autonom ui find --desc "Log In" --mode exact
autonom ui tap --desc "Continue"
autonom screenshot --label "after login"
autonom logs tail --package com.example.app --since 60
autonom location get                        # current position (Android)
autonom crash list
autonom open "myapp://order/42"

# Everything above is journaled; add a note, read the run back, then stop
autonom note add "login works; the 500 retry path is missing"
autonom journal
autonom session stop
```

Sessions live under `~/.autonom/sessions/<id>/` (machine-global, found from any
directory), and every verb plus your notes are appended to that session's
`journal.ndjson`. Every command prints JSON; expected failures print
`{"ok": false, "error_code": "...", "hint": "..."}` on stderr with exit code 2,
so an agent branches on a stable code instead of parsing prose.

See `docs/CAPABILITIES.md` for the full shipped vs planned matrix.

## Included skills

### Routing and shared workflow

- `autonom` — start here: the whole-system map (what it does, how, how to use it)
- `project-router` — narrows that map to the stack-specific skills for a task
- `toolchain-doctor`
- `autonom-setup` — build, carry, and install the harness itself
- `mobile-session` — explicit target session, global `~/.autonom/sessions/` artifacts, journal
- `mobile-screen` — accessibility tree, find/tap/gestures, screenshot
- `mobile-network` — consent-gated HTTP(S) capture, mocking, HAR export
- `mobile-flow` — repeatable Flow v1 files: strict YAML, exact selectors, polling assertions, failure classes
- `mobile-memory` — per-app knowledge and flow runbooks under `~/.autonom/apps/`

### Flutter

- `flutter-debugger-agent`
- `flutter-testing`
- `flutter-performance-audit`
- `flutter-memory-leaks`
- `flutter-android-platform`
- `flutter-release-validation`

### iOS

- `ios-project-setup`
- `ios-debugger-agent`

### Native Android and Compose

- `android-project-setup`
- `android-debugger-agent`
- `android-emulator-browser`
- `android-app-actions`
- `android-runtime-performance`
- `android-memory-leaks`
- `compose-performance-audit`

## Install

### One command

```bash
./install.sh          # checkbox picker: device tools, Claude, Codex, Grok
./install.sh --all    # headless: everything
./install.sh claude codex
```

The `autonom` CLI always lands on PATH; device tools and each agent are
opt-in checkboxes (flags when there is no terminal). The same script ships
inside the release bundle. Per-agent routes below do the same one layer at a
time.

### Codex

```bash
codex plugin marketplace add aiatsuk/autonom
codex plugin add autonom@autonom
codex plugin list
```

Local development:

```bash
codex plugin marketplace add /absolute/path/to/autonom
codex plugin add autonom@autonom
```

### Claude Code

```bash
claude plugin marketplace add aiatsuk/autonom   # or /absolute/path/to/autonom
claude plugin install autonom@autonom
```

From a checkout: `./scripts/install_claude.sh` (same commands; falls back to
loose skills in `~/.claude/skills` when the `claude` CLI is absent). Skills
load namespaced as `autonom:<name>`.

### Grok

```bash
./scripts/install_skills.sh grok
```

### Any skill-compatible agent

```bash
./scripts/install_skills.sh --link /path/to/agent/skills
# or a durable copy:
./scripts/install_skills.sh --copy /path/to/agent/skills
```

See `docs/INSTALL.md` for uninstall, prefixes, and details.

## Local validation

```bash
./scripts/run_checks.sh
# or
make check
```

Checks do not require Flutter or the Android SDK. Domain skills discover those
tools only when a target project needs them.

## Emulator browser

```bash
node plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs \
  --serial emulator-5554
```

The command prints a tokenized `127.0.0.1` URL. Open that exact URL in your
agent’s browser side panel. When `ffmpeg` and device-side H.264 streaming are
available, `--transport auto` uses them. Otherwise the bridge falls back to a
persistent screenshot stream.

Browser mirroring is visual proof and interaction support, not a performance
benchmark. Use a physical device plus native profilers for frame-rate review.

## Documentation

- `docs/INSTALL.md` — Codex / Claude / Grok / generic install
- `docs/CAPABILITIES.md` — Android/iOS capability matrix, full CLI surface, environment overrides
- `docs/ARCHITECTURE.md` — routing, CLI control plane, evidence, security
- `docs/COMPATIBILITY.md` — the frozen CLI contract (error codes, response keys, exit codes)
- `docs/FLOW.md` — Flow v1: the strict flow language, runner, recording, reports, atlas, proof
- `docs/plans/PHASE_0_RELEASE_ENGINEERING.md` — shipped: test isolation, CI, tagged releases
- `docs/plans/PHASE_4_METRICS.md` — shipped as 0.27.x: live session watch + metrics/memory/CPU/traces
- `docs/USAGE.md` — ready-to-use prompts and workflows
- `CHANGELOG.md` — release history
- `SECURITY.md` — the enforced security model (MITM, consent, redaction)
- `AGENTS.md` — short operating contract for any consuming agent

## Repository policy

- Preserve the target repository’s architecture and dependency conventions.
- Select one explicit device whenever more than one target is attached.
- Use the narrowest useful checks first, then broaden when shared behavior or a
  platform boundary changed.
- Separate code-backed findings from runtime evidence.
- Compare performance and memory captures only when device, build mode, flow,
  and configuration are equivalent.
- Never expose signing secrets, tokens, keystores, or `.env` contents in logs.

## License

MIT. See `LICENSE`.
