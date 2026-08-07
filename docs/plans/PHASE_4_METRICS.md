# Phase 4: Metrics, memory, and live session observation

**Status:** planning only (no implementation in this document).  
**Goal:** teach Autonom to **observe what an app is doing** while a session runs:

1. **Load** — memory, CPU, frames, heavier profiles (Android, iOS Simulator, Flutter).
2. **Live streams** — session output files, device logs, network requests — without
   forcing the agent (or human) to invent paths like
   `tail -f ~/.autonom/sessions/…/output/….log`.

**Depends on:** multi-platform session + target model (shipped), journal/artifacts
(shipped), `logs tail` / `network requests list` (shipped, snapshot-oriented),
Android skill helpers for meminfo/simpleperf/frame timings (shipped as scripts,
not CLI).  
**Target versions (proposed):**

| Version | Theme |
| --- | --- |
| **0.16.0** | **Live session watch** (`session outputs`, `logs follow`, `network requests` poll/follow) + `metrics snapshot` / `series` |
| **0.17.0** | `metrics memory capture` / `analyze`; Android gfxinfo + simpleperf traces |
| **0.18.0** | iOS `xctrace` traces; honest limitations; skill rewrites to call CLI |
| **0.19.0** | Flutter frame summary + optional VM Service attach (narrow) |

---

## 0. Why this phase exists

Agents already own a session, drive UI, sample logs, and list network requests.
Two gaps remain:

**A. Load / resources** — they cannot ask the harness “what is memory/CPU/jank
doing?” without raw `adb` / `xctrace` / DevTools.

**B. Live observation** — useful streams already land under the session tree
(e.g. `output/flutter_run_mitm.log`, log-stream files, mitm flows), but the
agent must **know absolute paths** and shell out to `tail -f`. Network
`requests list` is a point-in-time poll, not a follow. Humans and agents both
want:

```bash
# today (works, but brittle path knowledge)
tail -f ~/.autonom/sessions/s_6d64a1ee28/output/flutter_run_mitm.log
autonom network requests list --max 50

# target (session-relative, discoverable, followable)
autonom session outputs
autonom logs follow --source output:flutter_run_mitm.log
autonom network requests follow --max 50
```

This phase closes both gaps with:

1. A first-class **`autonom metrics …`** verb family (load).
2. A first-class **live observation** surface (`session outputs`, `logs follow`,
   network follow/poll ergonomics) over **session artifacts + device streams**.
3. Platform backends that wrap existing OS tools (no new daemons where avoidable).
4. Stable **JSON + artifacts** so before/after, series, and live tails fit the
   same evidence model as network/journal.
5. Skills that become **thin CLI callers**, not parallel shell encyclopedias.

### 0.1 Non-goals (explicit)

| Out of scope now | Why |
| --- | --- |
| Physical iOS device metrics | Signing, Developer Mode, `devicectl` policy — sim-only harness |
| Claiming “leak proven” from PSS/RSS growth | Requires retaining paths / Instruments Leaks with human or specialized analysis |
| Full Perfetto GUI replacement | Too large; ship presets later if needed |
| Web/WebSocket live dashboards in a browser | CLI + optional later MCP; not a GUI product |
| Multiplexed TUI like `multitail` as the only UI | Provide CLI follow; humans may still `tail -f` via printed paths |
| Root-only host tools (`powermetrics`) | Friction and privilege; optional later |
| XCUITest / Maestro runner | Separate phase; not metrics |
| React Native–specific metric stack | Planned domain skills later |

### 0.2 Design principles

1. **CLI is source of truth** — skills call `autonom metrics …` / `logs follow` /
   `session outputs`; helpers under `scripts/` become library code behind the CLI.
2. **Honest platform semantics** — Android `dumpsys meminfo` and iOS host
   `RSS` are **not** the same metric. Every metrics response carries
   `limitations[]` and `metric_semantics`.
3. **Same envelope as the rest of Autonom** —
   `{"ok": true|false, …}` / stderr error with `error_code` / exit 2.
   **Follow modes** are the exception: they may stream **NDJSON lines** on
   stdout (one event per line) until SIGINT or `--max-seconds`, then a final
   summary JSON on stderr or a last NDJSON `{"kind":"eof",…}` — see §2L.
4. **Session-aware artifacts** — metrics under
   `~/.autonom/sessions/<id>/metrics/`; live sources discovered from
   `session.json` + `output/`, `logs/`, `network/`.
5. **Journal measurable actions** — snapshot/trace/capture get journal lines;
   high-frequency follow frames do **not** spam the journal (optional
   `follow start/stop` bookends only).
6. **Dependency-light** — stdlib Python; shell out to `adb`, `xcrun xctrace`,
   `sample`/`vmmap`, `simpleperf`, existing parsers; file follow via inotify
   optional, poll fallback everywhere.
7. **Evidence ladder** — live watch for orientation → snapshot/series → heavy
   trace; never skip to “it’s a leak” from one number or one log line.
8. **Doctor reports capability**, not failure of the whole harness when
   `xctrace` is missing.
9. **Paths are always printed** — even when `logs follow` exists, responses
   include `path` so a human can still `tail -f` that file in another terminal.

---

## 1. Problem statement

### 1.1 Load / metrics

Agents need answers to:

| Question | Android evidence | iOS Simulator evidence | Flutter evidence |
| --- | --- | --- | --- |
| Is memory growing? | `dumpsys meminfo`, `/proc`, HPROF | host RSS/`vmmap`, Allocations `.trace` | Dart heap (DevTools/VM), plus platform layer |
| Is the process hot? | `cpuinfo` / `top` / simpleperf | host `sample` / Time Profiler | UI isolate vs platform |
| Are frames janky? | `gfxinfo` framestats, Perfetto | Animation Hitches / Core Animation (xctrace) | profile-mode frame timings JSON |
| Disk / container bloat? | app data paths (limited) | `get_app_container` + `du` | same + build size separate |
| Native alloc churn? | heapprofd | Allocations/Leaks templates | JNI/plugin via platform tools |

**One metrics CLI surface, three backends**, with a shared “summary” schema and
platform-specific `raw` / `artifacts` extensions.

### 1.2 Live session observation

While a flow runs, agents and humans need to **watch streams** without hunting
filesystem layout:

| Stream | What exists today | Gap |
| --- | --- | --- |
| Session process output | Files under `…/sessions/<id>/output/` (e.g. `flutter_run_mitm.log`, launcher stdout) | Path not listed by CLI; only raw `tail -f` |
| Device logs | `autonom logs tail` (snapshot window) | No **follow**; iOS `--log-stream` file not first-class |
| Network | `autonom network requests list --max 50` | Point-in-time only; no follow/poll helper; easy to miss “what just fired” |
| Journal | `autonom journal` | Snapshot of actions; no follow of new actions/notes |
| Combined “what’s happening” | Manual multi-terminal | No single discoverability entrypoint |

**Target experience:**

```bash
autonom session show
# → artifacts_dir, live sources summary

autonom session outputs
# → catalog of followable files (output/*, logs/*, network/*)

# Live file (replaces hand-rolled tail -f on known path)
autonom logs follow --source output:flutter_run_mitm.log
# or
autonom logs follow --path output/flutter_run_mitm.log

# Live device log (Android logcat -v … / iOS stream file or log stream)
autonom logs follow --source device --package com.example.app

# Network: keep polling / follow new flows
autonom network requests list --max 50
autonom network requests follow --max 20 --interval 1

# Optional multiplex (later): one NDJSON mixer
autonom session watch --sources output:flutter_run_mitm.log,network,device-log
```

Humans may still open a second terminal with the **printed absolute path**:

```bash
tail -f ~/.autonom/sessions/s_6d64a1ee28/output/flutter_run_mitm.log
```

The CLI must make that path **discoverable**, not mandatory to memorize.

---

## 2. Agent-facing CLI design

### 2.1 Verb tree

```bash
# --- Live session observation (0.16, alongside metrics foundation) ---
autonom session outputs              # catalog followable session files
autonom session watch [options]      # optional multiplex follow (can ship after follow primitives)
autonom logs tail    [options]       # exists — keep; align flags with follow
autonom logs follow  [options]       # NEW: stream session file or device log
autonom network requests list        # exists — ensure --max, since_id, clear filters
autonom network requests follow      # NEW: poll store for new flows as NDJSON
autonom journal follow               # NEW (thin): new journal lines as NDJSON

# --- Metrics / load ---
autonom metrics snapshot   [options]   # one point-in-time summary
autonom metrics series     [options]   # N snapshots + deltas / leads
autonom metrics memory capture|analyze # Android-first pack; iOS subset
autonom metrics frames     [options]   # Android gfxinfo; Flutter JSON later
autonom metrics trace      [options]   # heavy profile (simpleperf / xctrace)
autonom metrics list-presets           # what this host can run
```

Metrics verbs accept the global target flags: `--platform`, `--target` /
`--serial` / `--udid`, and prefer **session** when present. Live observation
verbs default to the **current session** (`session show`); `--session-id` or
`--artifacts-dir` override.

Common metrics options:

| Flag | Meaning |
| --- | --- |
| `--app-id` / package | Required if not in session |
| `--out DIR\|FILE` | Artifact destination (default: session `metrics/`) |
| `--json` | Always on for machine use; keep consistent with other verbs (stdout JSON) |
| `--label` | Short name in filenames + journal |
| `--task` | Optional task id for grouping (same as screenshots) |

---

## 2L. Live observation CLI (detail)

### 2L.1 Session layout conventions (stabilize)

Document and enforce a stable tree so discovery is reliable:

```text
~/.autonom/sessions/<session_id>/
  session.json
  journal.ndjson
  output/                    # process stdout/stderr captures (flutter run, mitmdump tee, …)
    flutter_run_mitm.log
    …
  logs/                      # device log streams when --log-stream / adb logcat tee
    stream.log
    logcat.log
  network/
    flows.ndjson             # or store backend files
    proxy.json
  metrics/                   # Phase 4 metrics artifacts
  screenshots/
```

**Rules for writers (session start, network start, flutter wrappers):**

1. Anything long-lived that an agent might want to watch **must** register a
   file under `output/` or `logs/` (or network store).
2. Prefer **append-only** text or NDJSON.
3. Record the relative path in `session.json` → `streams[]` when known at start:

```json
"streams": [
  {
    "id": "flutter_run_mitm",
    "kind": "output",
    "path": "output/flutter_run_mitm.log",
    "label": "flutter run + mitm",
    "pid": 12345
  },
  {
    "id": "log_stream",
    "kind": "device_log",
    "path": "logs/stream.log",
    "label": "ios log stream"
  },
  {
    "id": "network_flows",
    "kind": "network",
    "path": "network/flows.ndjson",
    "label": "mitm flows"
  }
]
```

If older sessions lack `streams[]`, **scan** `output/`, `logs/`, `network/` by
convention (glob `*.log`, `*.ndjson`).

### 2L.2 `session outputs`

```bash
autonom session outputs
autonom session outputs --session-id s_6d64a1ee28
```

```json
{
  "ok": true,
  "session_id": "s_6d64a1ee28",
  "artifacts_dir": "/Users/…/.autonom/sessions/s_6d64a1ee28",
  "streams": [
    {
      "id": "flutter_run_mitm",
      "kind": "output",
      "path": "output/flutter_run_mitm.log",
      "abs_path": "/Users/…/.autonom/sessions/s_6d64a1ee28/output/flutter_run_mitm.log",
      "bytes": 182334,
      "mtime": "2026-08-07T12:01:02Z",
      "follow_hint": "autonom logs follow --source output:flutter_run_mitm.log",
      "shell_hint": "tail -f '/Users/…/.autonom/sessions/s_6d64a1ee28/output/flutter_run_mitm.log'"
    }
  ]
}
```

Every entry includes **`abs_path`** + **`shell_hint`** so a human can open a
second terminal without guessing.

### 2L.3 `logs follow`

Unify “tail a session file” and “follow device logs”:

```bash
# Session file by registered id or relative path
autonom logs follow --source output:flutter_run_mitm.log
autonom logs follow --path output/flutter_run_mitm.log
autonom logs follow --source logs:stream.log

# Device log (platform backend)
autonom logs follow --source device --package com.example.app
autonom logs follow --source device --grep 'Exception|Error'

# Limits (agents must not hang forever without a bound in CI)
autonom logs follow --source output:flutter_run_mitm.log --max-seconds 120 --max-lines 500
```

| Flag | Meaning |
| --- | --- |
| `--source` | `output:<name>`, `logs:<name>`, `device`, or stream `id` from `session outputs` |
| `--path` | Relative to artifacts_dir or absolute file |
| `--from-start` | Replay existing file then follow (default: start at EOF, like `tail -f`) |
| `--max-seconds` | Stop after N seconds (0 = until SIGINT; **tests/CI must set N**) |
| `--max-lines` | Stop after N emitted lines |
| `--grep` | Regex filter (line must match) |
| `--poll-ms` | Poll interval when inotify unavailable (default 200–500) |

**Stdout protocol (follow modes):** NDJSON, one object per line:

```json
{"kind":"line","source":"output:flutter_run_mitm.log","ts":"…","text":"flutter: …"}
{"kind":"line","source":"device","ts":"…","text":"08-07 12:00:01.234  …"}
{"kind":"eof","reason":"max_seconds","lines":120}
```

Exit 0 on clean EOF limits; exit 130/2 on error with final stderr JSON
`{"ok":false,"error_code":…}` if the stream never opened.

**Android `device`:** `adb logcat` without `-d` (streaming), optional `--pid`
from package.  
**iOS `device`:** prefer existing session `logs/stream.log` if `--log-stream`
was used; else spawn `log stream` with bundle predicate (document cost).

### 2L.4 `network requests list` (ergonomics) + `follow`

**Keep and harden list** (already shipped):

```bash
autonom network requests list --max 50
autonom network requests list --max 50 --host api.example.com --status 500
autonom network requests list --since-id f_0120
```

Add if missing:

| Flag | Meaning |
| --- | --- |
| `--max` | Cap results (user example: 50) |
| `--since-id` | Only flows after this id (pagination / poll) |
| `--since-ts` | Only flows after ISO timestamp |

**NEW follow:**

```bash
autonom network requests follow --interval 1 --max 50
autonom network requests follow --host api.example.com --max-seconds 180
```

Implementation: poll the flow store every `--interval` seconds; emit NDJSON
for **new** flow ids only (same redaction as list/show). Final `{"kind":"eof",…}`.

This replaces the agent loop:

```text
loop: network requests list --max 50 → diff mentally → sleep
```

### 2L.5 `journal follow` (thin)

```bash
autonom journal follow --max-seconds 300
```

Tail `journal.ndjson` as NDJSON lines (already one JSON per line). Low effort;
high value when correlating UI actions with network/output.

### 2L.6 `session watch` (optional multiplex, after primitives)

```bash
autonom session watch \
  --sources output:flutter_run_mitm.log,network,device \
  --max-seconds 300
```

Merges multiple follows into one NDJSON stream with a `source` field. Ship only
after `logs follow` + `network requests follow` are solid. Not required for MVP.

### 2L.7 Agent + human dual workflow

| Actor | Recipe |
| --- | --- |
| Human (second terminal) | `autonom session outputs` → copy `shell_hint` → `tail -f …` |
| Agent (bounded) | `logs follow --max-seconds 60` while driving UI in another step; or poll `network requests list --since-id` |
| Agent (CI) | Never unbounded follow; always `--max-seconds` / `--max-lines` |
| Correlation | `journal` + network ids + output timestamps in the report |

### 2L.8 How to implement follow (learning / engineering)

| Step | Work |
| --- | --- |
| File tail | Open file, seek EOF (or start), read loop; macOS/Linux poll if no watchdog |
| Rotation | If inode/size shrinks, reopen (log rotation) |
| Registration | On `session start --log-stream`, `network start`, future `flutter run` helpers: append `streams[]` + open files under `output/` / `logs/` |
| Back-compat | Sessions without `streams[]`: directory scan |
| Tests | Temp dir with growing file; assert NDJSON lines; max-seconds stops; grep filters |
| Security | Only paths under `artifacts_dir` (reject `..`); same confinement as `file pull` |

### 2L.9 Acceptance (live observation)

- [ ] `session outputs` lists `output/flutter_run_mitm.log` style files with
      `abs_path` and `shell_hint` matching a real `tail -f` path.
- [ ] `logs follow --path output/….log --from-start --max-lines 5` emits ≥1 NDJSON
      line on a non-empty fixture file.
- [ ] `logs follow --max-seconds 1` exits 0 with `kind=eof`.
- [ ] `network requests list --max 50` remains stable; `follow` emits only new ids.
- [ ] Path escape outside artifacts_dir → `error_code: path_forbidden`.

---

### 2.2 `metrics snapshot`

**Purpose:** cheapest answer to “how is the app right now?”

**Android (required fields when available):**

```json
{
  "ok": true,
  "platform": "android",
  "target_id": "emulator-5554",
  "app_id": "com.example.app",
  "pid": 4321,
  "captured_at": "2026-08-07T12:00:00Z",
  "metric_semantics": "android_dumpsys_meminfo_v1",
  "memory": {
    "total_pss_kb": 9000,
    "java_heap_kb": 4000,
    "native_heap_kb": 2000,
    "graphics_kb": 800,
    "activities": 1,
    "views": 120
  },
  "cpu": {
    "available": true,
    "note": "short top/cpuinfo sample",
    "process_percent": 12.5
  },
  "proc": {
    "threads": 42,
    "vm_rss_kb": 8500
  },
  "limitations": [],
  "artifacts": ["…/metrics/20260807T120000Z-snapshot.json"]
}
```

**iOS Simulator:**

```json
{
  "ok": true,
  "platform": "ios",
  "target_id": "AAAA-…",
  "app_id": "com.example.app",
  "pid": 5566,
  "captured_at": "…",
  "metric_semantics": "ios_simulator_host_process_v1",
  "memory": {
    "rss_bytes": 188743680,
    "source": "host_ps_or_top"
  },
  "cpu": {
    "process_percent": 8.2,
    "window_s": 1.0,
    "source": "host_top"
  },
  "disk": {
    "data_container_bytes": 52428800,
    "source": "simctl_get_app_container+du"
  },
  "limitations": [
    "RSS is the host view of the Simulator process, not guest jetsam accounting",
    "Not comparable 1:1 to Android total_pss_kb"
  ],
  "artifacts": ["…"]
}
```

**Failure modes (stable `error_code`s):**

| Code | When |
| --- | --- |
| `app_not_running` | No pid / package not alive |
| `tool_missing` | adb / xcrun / sample missing |
| `target_ambiguous` | Multiple devices, no session/target |
| `unsupported_platform` | e.g. metrics on unsupported future target |
| `permission_denied` | rare host tool refusal |

### 2.3 `metrics series`

```bash
autonom metrics series --interval 2 --count 10 --label scroll-feed
```

Behavior:

1. Take `count` snapshots spaced by `interval` seconds (agent may drive UI
   between calls in a loop *or* we support `--during` later; **v1 = passive
   series** while the agent drives in another turn).
2. For each metric key that is numeric, compute `first`, `last`, `delta`,
   `min`, `max`, `slope_per_sample`, `decreases`, `directional_growth` —
   **reuse the algorithm** already proven in
   `analyze_meminfo_series.py`.
3. Emit `directional_growth_leads` + fixed interpretation string:

> Directional trend only. A leak requires a retained object/root path or a
> repeatable accumulation pattern under an equivalent flow.

v1 also supports **offline** series from a directory of prior snapshots:

```bash
autonom metrics series --from-dir ~/.autonom/sessions/…/metrics --glob '*-snapshot.json'
```

### 2.4 `metrics memory capture` / `analyze`

Lift Android skill helpers into the CLI:

```bash
autonom metrics memory capture --label after-checkout [--no-hprof]
autonom metrics memory analyze [--glob '*-meminfo.txt'] [--min-growth-kb 1024]
```

- **capture** wraps today’s `capture_android_memory.sh` logic in Python
  (`autonom_lib/metrics/android_memory.py`): meminfo, proc status, gfxinfo,
  optional `am dumpheap`.
- **analyze** wraps `analyze_meminfo_series.py` as a library.
- **iOS capture (v1 thin):** snapshot + optional `idb simulate-memory-warning`
  as `metrics memory warn` (stimulus only). Full Allocations go under `trace`.

### 2.5 `metrics frames`

**Android:**

```bash
autonom metrics frames reset
# agent runs UI flow
autonom metrics frames capture   # dumpsys gfxinfo framestats → parse summary
```

Summary JSON: janky frames count if parseable, percentile-ish stats where
stable, always keep **raw artifact**.

**Flutter (0.19):**

```bash
autonom metrics frames flutter-summary path/to/integration_response.json
```

Reuses `frame_timings_summary.py` logic (build/raster, budget, over_budget).

**iOS frames (0.18+):** only via `metrics trace --preset hitches` (xctrace),
not a fake gfxinfo.

### 2.6 `metrics trace`

Heavy, explicit duration, always an artifact:

```bash
# Android
autonom metrics trace --preset simpleperf --duration 30
autonom metrics trace --preset gfxinfo-flow   # document: reset → user flow → capture

# iOS
autonom metrics trace --preset allocations --duration 30
autonom metrics trace --preset time-profiler --duration 30
autonom metrics trace --preset leaks --duration 30
autonom metrics trace --preset hitches --duration 30
```

| Preset | Backend | Output |
| --- | --- | --- |
| `simpleperf` | adb simpleperf record + host report scripts | `perf.data` + text reports |
| `gfxinfo-flow` | dumpsys gfxinfo | framestats text + summary JSON |
| `allocations` | `xctrace` template Allocations | `.trace` |
| `time-profiler` | `xctrace` Time Profiler | `.trace` |
| `leaks` | `xctrace` Leaks | `.trace` |
| `hitches` | `xctrace` Animation Hitches (or Hitches) | `.trace` |

Optional:

```bash
autonom metrics trace --preset allocations --export-xml
# xctrace export best-effort; if export shape is unstable, ship artifact only
```

v1 success = **artifact on disk + journal entry**, not full stack parsing.

### 2.7 `metrics list-presets`

```json
{
  "ok": true,
  "platform": "ios",
  "presets": [
    {"id": "allocations", "available": true, "tool": "xctrace"},
    {"id": "simpleperf", "available": false, "reason": "android_only"}
  ],
  "tools": {
    "xctrace": true,
    "sample": true,
    "vmmap": true,
    "idb": true
  }
}
```

---

## 3. How to *learn* / implement each signal

This section is the “how we teach the harness to read it” plan — tooling,
algorithm, tests, and honesty.

### 3.1 Android memory (PSS / heaps / object counts)

| Step | Work |
| --- | --- |
| Source | `adb shell dumpsys meminfo <package>` |
| Parse | Port `METRIC_PATTERNS` from `analyze_meminfo_series.py` into
  `autonom_lib/metrics/meminfo.py` |
| Enrich | `/proc/<pid>/status` → Threads, VmRSS, VmSize |
| Optional | `am dumpheap` when debuggable; surface path or skip with reason |
| Tests | Existing meminfo fixtures + golden JSON for parser |
| Proof rule | Document: growth ≠ leak |

**Learning checklist for implementers:**

1. Capture 3 real meminfo dumps from an emulator (idle / after list scroll /
   after navigate back).
2. Confirm parser keys against fixtures.
3. Run series algorithm; verify leads only when delta ≥ threshold and slope > 0.

### 3.2 Android CPU

| Step | Work |
| --- | --- |
| Quick | `adb shell top -n 1 -d 1` or `dumpsys cpuinfo` — parse line for package |
| Deep | simpleperf preset (existing shell scripts → Python orchestration) |
| Tests | Fake adb fixtures with canned top/cpuinfo |

**Honesty:** short top samples are noisy; series + fixed flow required for claims.

### 3.3 Android frames

| Step | Work |
| --- | --- |
| Source | `dumpsys gfxinfo <pkg> reset` then `framestats` |
| Parse | Best-effort summary; always store raw |
| Tests | Fixture from one device API level; skip parse if format unknown |

### 3.4 iOS memory / CPU (Simulator host view)

| Step | Work |
| --- | --- |
| Resolve pid | From `simctl launch` return value when we launched; else
  heuristics: `pgrep -f` bundle display name / executable name from
  `listapps` / `appinfo` |
| CPU/RSS | `ps -p pid -o %cpu,rss` or `top -l 2 -pid pid` (macOS) |
| Optional map | `vmmap -summary pid` → parse “Physical footprint” if present |
| Disk | `simctl get_app_container udid bundle data` + `du -sk` |
| Stimulus | `idb simulate-memory-warning` as separate verb |
| Tests | Fake pid resolver + canned `ps` output; no CI dependency on booted sim
  for unit tests |

**Critical learning:** document that Simulator metrics are **host process
accounting**. They are useful for before/after on the **same** sim/runtime, not
for comparing to Android PSS or to a physical device.

### 3.5 iOS heavy profiles (`xctrace`)

| Step | Work |
| --- | --- |
| Detect | `xcrun xctrace version` / list templates in doctor |
| Record | `xctrace record --template T --device UDID --attach name|pid --time-limit`
  or `--launch` when starting cold |
| Attach policy | Prefer attach to running session app; fail clearly if not running |
| Output | Copy/move `.trace` into session metrics dir |
| Export | Optional `xctrace export`; treat as best-effort |
| Tests | Unit-test argv builder; integration `@macOS` optional smoke if Xcode present |

**Learning checklist:**

1. Manually run Allocations + Time Profiler against a sample Simulator app.
2. Confirm attach by name vs pid on current Xcode.
3. Record failure strings when SIP / privacy prompts block recording; map to
   `error_code` + hint.

### 3.6 Flutter

| Layer | How Autonom learns it |
| --- | --- |
| Frame timings file | Reuse `frame_timings_summary.py` as library; CLI path arg |
| Profile mode discipline | Skill text: only profile/release-like claims |
| Dart heap | **0.19 optional:** discover VM service URI from `flutter run --machine`
  log in session; *or* accept `--vm-service-uri` from agent. v1 may only
  **document** DevTools and not automate heap snapshots |
| Platform escalation | If Dart heap flat but process RSS grows → Android/iOS snapshot |

Do **not** block 0.16–0.18 on VM Service. Ship platform metrics first.

### 3.7 Cross-cutting: process identity

Shared helper `autonom_lib/metrics/process.py`:

```text
resolve_app_process(platform, target, app_id) -> {pid, name, sources_tried[]}
```

Android: `pidof -s package`.  
iOS: session launch pid → `listapps` CFBundleExecutable → `pgrep`.  
Always return what was tried on failure (agent-debuggable).

---

## 4. Module architecture

```text
scripts/autonom.py
  # argparse only → dispatch

scripts/autonom_lib/
  session.py              # + streams[] registry; session outputs catalog
  follow.py               # NEW: file tail + poll loop + NDJSON emit + path guard
  logs.py                 # + follow device / session files
  network/store.py        # + since_id iteration for follow
  metrics/
    __init__.py
    schema.py             # shared JSON builders, limitations helpers
    process.py            # pid resolution
    snapshot.py           # orchestrate one snapshot
    series.py             # multi-snapshot + slope/leads (from meminfo series)
    android_meminfo.py    # parse dumpsys meminfo
    android_cpu.py        # top/cpuinfo best-effort
    android_frames.py     # gfxinfo
    android_trace.py      # simpleperf orchestration
    android_memory_pack.py
    ios_host.py           # ps/top/vmmap/du
    ios_xctrace.py
    ios_stimuli.py
    flutter_frames.py
    artifacts.py          # paths under session metrics/
```

Rules:

- `autonom.py` does not branch on platform beyond dispatch tables.
- No skill script remains the only entrypoint for a shipped capability; scripts
  may stay as thin wrappers calling the library for backwards compatibility.
- Fakes: extend `tests/fakes/` with `fake_xctrace`, canned meminfo already exists.
- Follow modes: never write unbounded journal lines per log line.

### 4.1 Doctor capabilities

Extend `autonom doctor` JSON:

```json
"metrics": {
  "android_meminfo": true,
  "android_simpleperf": false,
  "ios_host_sample": true,
  "ios_xctrace": true,
  "flutter_frame_summary": true
}
```

`--strict` does **not** fail solely because simpleperf/xctrace missing (optional
profilers). Fail only if core snapshot tools missing (adb/simctl/ps).

### 4.2 Session layout

```text
~/.autonom/sessions/<id>/
  metrics/
    20260807T120000Z-label-snapshot.json
    20260807T120030Z-label-meminfo.txt      # android raw
    20260807T120100Z-label-allocations.trace
    series-label.json
  journal.ndjson   # kind=action verb=metrics.snapshot|metrics.trace|…
```

---

## 5. Skills and docs updates (after CLI lands)

| Skill | Change |
| --- | --- |
| `android-memory-leaks` | Prefer `autonom metrics memory *` / `snapshot` / `series` |
| `android-runtime-performance` | Prefer `autonom metrics frames` / `trace --preset simpleperf` |
| `flutter-memory-leaks` | Platform escalation via `metrics snapshot` both platforms |
| `flutter-performance-audit` | `metrics frames flutter-summary` |
| `ios-debugger-agent` | New section: metrics ladder; remove “not part of skill” for snapshots |
| `mobile-session` | Mention metrics artifacts dir |
| `autonom` (entry skill) | Map metrics in the whole-system diagram |
| `docs/CAPABILITIES.md` | Mark snapshot/series/trace shipped per version |
| `docs/USAGE.md` | “Investigate memory / CPU” recipes using CLI only |
| `docs/ARCHITECTURE.md` | Add metrics module to control-plane map |

---

## 6. Evidence ladder (agent procedure)

```text
0. Own a session (explicit target, app running).
   session outputs           → see what is followable (flutter log, log stream, …)
   Optional second terminal: tail -f <abs_path from shell_hint>
1. metrics snapshot          → baseline
2. Drive ONE fixed UI flow (ui tap/swipe/…).
   In parallel (bounded): logs follow --max-seconds …  and/or
   network requests list --max 50  /  network requests follow
3. metrics snapshot          → after
4. If delta interesting: metrics series while repeating the flow
5. If still unclear:
     Android → memory capture + analyze; optional trace simpleperf/gfxinfo
     iOS     → trace allocations / time-profiler / leaks
     Flutter → profile mode + frames summary; then platform metrics
6. Journal + screenshots bookend the flow
7. Report: numbers + live observations + artifacts + limitations + what is NOT proven
```

**Comparison contract** (must appear in skill + USAGE):

- Same device/simulator runtime, app build, thermal state, account/data seed.
- Same flow steps (journal/UI script).
- State units and `metric_semantics`.
- Never compare Android `total_pss_kb` to iOS `rss_bytes` as equivalent.

---

## 7. Phased delivery plan

### 7.1 0.16.0 — Live session watch + metrics snapshot/series foundation

Ship **live observation first or in parallel** with metrics snapshot — agents
use it every run; metrics are for deeper load investigation.

**Deliverables — live observation**

- [ ] Stabilize session dirs: `output/`, `logs/`, optional `session.json` → `streams[]`
- [ ] Writers register streams (log-stream, network, documented flutter/mitm output)
- [ ] `autonom session outputs` (catalog + `abs_path` + `shell_hint` for `tail -f`)
- [ ] `autonom logs follow` (session file + device; NDJSON; `--max-seconds` / `--max-lines`)
- [ ] `autonom network requests list` flags: `--max`, `--since-id` (if not already)
- [ ] `autonom network requests follow` (poll new flows as NDJSON)
- [ ] Optional: `journal follow`
- [ ] Path confinement under `artifacts_dir`
- [ ] Tests: growing tempfile follow, max-seconds, grep, path_forbidden
- [ ] USAGE recipe: “watch session live” with both CLI follow and human `tail -f`

**Deliverables — metrics foundation**

- [ ] `autonom_lib/metrics/` skeleton + schema + artifacts + journal hooks
- [ ] Android snapshot (meminfo + proc + best-effort cpu)
- [ ] iOS snapshot (pid + cpu/rss + data container size)
- [ ] `metrics series` (live + `--from-dir`)
- [ ] `metrics list-presets` (minimal)
- [ ] Doctor metrics block
- [ ] Unit tests with fixtures/fakes
- [ ] CAPABILITIES + entry skill blurb

**Acceptance**

- Live: `session outputs` shows a registered `output/*.log` with a working
  `shell_hint`; `logs follow --max-seconds 1` exits cleanly; `network requests
  list --max 50` works; follow emits only new flow ids in a unit test with a
  fake store.
- Metrics: Android snapshot has `total_pss_kb` or clear error; iOS snapshot has
  `pid` + `rss_bytes` or clear error; series math matches meminfo fixtures.
- `run_checks.sh` green.

**Est.:** 4–6 days (live ~2 days + metrics ~2–4 days; can split PRs).

### 7.2 0.17.0 — Android memory pack & frames/CPU depth

**Deliverables**

- [ ] `metrics memory capture|analyze` (Python port of shell helpers)
- [ ] `metrics frames reset|capture` (gfxinfo)
- [ ] `metrics trace --preset simpleperf`
- [ ] Deprecate skill-only paths in docs (keep scripts as wrappers)
- [ ] Integration tests with fake adb for capture argv

**Acceptance**

- Capture writes meminfo+metadata under session metrics without skill path.
- Simpleperf preset fails with `tool_missing` + install hint when absent;
  succeeds on machine with NDK simpleperf (manual).

**Est.:** 2–3 days.

### 7.3 0.18.0 — iOS xctrace + stimuli

**Deliverables**

- [ ] `metrics trace --preset allocations|time-profiler|leaks|hitches`
- [ ] `metrics memory warn` (simulate-memory-warning)
- [ ] Optional host `sample` text artifact (`--include-sample-seconds N`)
- [ ] ios-debugger-agent + autonom skill updates
- [ ] macOS-marked smoke test or argv-only unit tests + manual checklist

**Acceptance**

- With Xcode: 10s Time Profiler attach produces `.trace` in metrics dir and
  journal entry.
- Without Xcode tools: preset lists `available: false` with hint.

**Est.:** 2–4 days.

### 7.4 0.19.0 — Flutter frames (+ optional VM)

**Deliverables**

- [ ] `metrics frames flutter-summary <file>`
- [ ] Document profile-mode + DevTools memory workflow in skills
- [ ] Optional: `--vm-service-uri` probe that only checks reachability / isolates
  list (no full heap dump automation required)
- [ ] CAPABILITIES: Flutter VM row partial/shipped as appropriate

**Acceptance**

- Fixture `frame_timings.json` yields same numbers as current unit tests via CLI.

**Est.:** 1–2 days (+ more if VM heap automation expands).

---

## 8. Testing strategy

| Layer | What |
| --- | --- |
| Unit | Parsers (meminfo, ps, gfxinfo best-effort), series math, argv builders |
| Fake backends | fake_adb responses; fake_xctrace recording argv + touch empty .trace |
| Contract | Snapshot key presence per platform (golden optional) |
| Manual macOS | Boot sim, run app, snapshot + 15s allocations trace |
| Manual Android | Emulator package, snapshot + series during ui swipe loop |
| Non-regression | Full `run_checks.sh` |

TTY/consent: metrics must **not** require interactive consent (unlike network
MITM). They are read-only observation of a target the agent already owns.

---

## 9. Error vocabulary (additions)

| error_code | Meaning |
| --- | --- |
| `app_not_running` | Cannot resolve pid / package not alive |
| `tool_missing` | Required binary missing (include `tool` field + hint) |
| `preset_unavailable` | Preset not for platform or tool missing |
| `trace_failed` | xctrace/simpleperf non-zero; include stderr tail |
| `parse_partial` | Snapshot ok but some fields missing (still `ok: true` with warnings?) |

**Decision:** prefer `ok: true` with `warnings[]` for partial snapshots (e.g. cpu
unavailable but memory ok). Use `ok: false` only when no useful signal was
captured.

---

## 10. Security & privacy

- Metrics artifacts can contain **package names, PIDs, heap graphs paths** —
  keep under session dir; do not echo HPROF contents.
- Never print keystores or user content from app containers when measuring disk.
- Redaction: not as aggressive as network; still avoid dumping full `vmmap` to
  stdout (artifact file only, summary in JSON).
- Journal stores verb + label + artifact paths, not full meminfo text.

---

## 11. Rollout & migration

1. Land library + CLI without deleting skill scripts.
2. Point skills at CLI; keep scripts as wrappers for one minor version.
3. Bump plugin version so Claude/Codex caches refresh.
4. Update `CAPABILITIES.md` matrix cells from 🔜 to ✅/⚠️ per ship.

---

## 12. Open decisions (resolve before or during 0.16)

| # | Question | Recommendation |
| --- | --- | --- |
| D1 | Single verb `metrics` vs also `memory` top-level | **Only `metrics`** with subcommands |
| D2 | iOS pid resolution strategy | Session launch pid → executable name → pgrep |
| D3 | Parse xctrace export in v1? | **No** — artifact + optional export file |
| D4 | Block on Flutter VM Service? | **No** for 0.16–0.18 |
| D5 | Series interactive (CLI waits while agent drives)? | v1 passive; agent loops snapshot or uses interval series without UI automation inside CLI |
| D6 | Minimum Xcode / adb versions | Document “current stable Xcode + platform-tools”; doctor prints versions |

---

## 13. Success metrics for the phase

The phase is done when an agent can, **without reading skill script paths**:

1. Start an Android or iOS session, launch an app.
2. **Discover** followable session streams via `session outputs` and either
   `logs follow` or human `tail -f` using `shell_hint`.
3. While driving UI, **list/follow network requests** (`list --max 50` /
   `follow`) without ad-hoc path knowledge.
4. Take baseline and post-flow **metrics snapshots** via CLI.
5. Run a **series** and get directional leads JSON.
6. On Android, **capture memory pack** and **simpleperf/gfxinfo** traces into
   the session.
7. On iOS, record an **Allocations** or **Time Profiler** `.trace` into the
   session.
8. On Flutter, summarize **frame timing JSON** via CLI.
9. Explain limitations correctly (growth ≠ leak; iOS host ≠ Android PSS).

---

## 14. Related documents

| Doc | Role |
| --- | --- |
| `docs/CAPABILITIES.md` | Shipped vs planned matrix (update as versions land) |
| `docs/ARCHITECTURE.md` | Control-plane module map |
| `docs/USAGE.md` | Operator recipes |
| `docs/plans/PHASE_2_3_IOS_NETWORK.md` | Precedent for phased CLI design |
| Skills: `android-memory-leaks`, `android-runtime-performance`,
  `flutter-memory-leaks`, `flutter-performance-audit`, `ios-debugger-agent` | Domain procedures; become CLI-first |

---

## 15. Suggested first implementation tickets (0.16.0)

### Ticket A — Live session watch (do first)

**Title:** `session outputs` + `logs follow` + `network requests follow`

**Files:** `session.py` streams registry, `logs.py` follow, network store poll,
`autonom.py` argparse, tests, USAGE.

**Demo:**

```bash
autonom session start --platform ios --target <UDID> --launch --app-id com.example --log-stream
autonom session outputs
# human: tail -f "$(autonom session outputs | jq -r '.streams[0].abs_path')"
autonom logs follow --source logs:stream.log --max-seconds 30
autonom network start && autonom network attach
autonom network requests list --max 50
autonom network requests follow --interval 1 --max-seconds 60
```

Equivalent to today’s manual:

```bash
tail -f ~/.autonom/sessions/s_6d64a1ee28/output/flutter_run_mitm.log
autonom network requests list --max 50
```

…but with **discovery** and **bounded follow** for agents.

### Ticket B — Metrics snapshot + series

**Title:** `metrics snapshot` + `series` for Android meminfo and iOS host process  

**Files:** `autonom_lib/metrics/*`, `autonom.py` argparse, tests, USAGE blurb,
doctor.  

**Demo:**

```bash
autonom session start --platform android --serial emulator-5554 --launch --app-id com.example
autonom metrics snapshot --label baseline
# … ui flow …
autonom metrics snapshot --label after
autonom metrics series --from-dir "$ARTIFACTS/metrics" --glob '*-snapshot.json'
```

Then the same with `--platform ios --target <UDID>`.

---

*End of Phase 4 plan. Implementation should track checkboxes in §7 and flip
CAPABILITIES cells only when acceptance criteria pass.*
