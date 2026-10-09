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
   Node, shellcheck required) and `release` on `v*` tags. "Green" means CI
   green.

## Control plane

```text
Host agent  →  skills  →  scripts/autonom.py  →  adb / xcrun simctl / idb / axe / XCUITest / mitmdump
                              ↓
                     ~/.autonom/sessions/<id>/
```

### State model

Nothing lives in the project directory. Two machine-global roots, both
overridable by `AUTONOM_HOME`:

| Root | Default | Holds |
| --- | --- | --- |
| Session store | `~/.autonom/sessions/<id>/` | `session.json`, `journal.ndjson`, shots, trees, logs, network flows, recordings, crashes, pulled files |
| Machine state | `$XDG_STATE_HOME/autonom`, else `~/.local/state/autonom` | mock registry, process registry, mitmproxy confdir (CA key, mode `0700`), `simulator-state/<target>.json` and `simulator-prefs/<udid>.json` pin snapshots |
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
  journal.py                  append-only journal.ndjson: every verb, parser-resolved scrubbed argv, notes
  consent.py                  the consent gate: per-invocation flag + typed phrase, never cached
  selector.py                 shared selector matching and duplicate policy
  ui.py                       platform dispatch + the compact node schema
  ui_android.py / adb.py      UI Automator parsing and adb actuation
  ui_ios.py / ios_idb.py      accessibility tree parsing; idb actuation, or AXe for input
  ios_simctl.py               simulator lifecycle, screenshots, logs, device state
  emulator.py                 AVD discovery, emulator boot (registered harness-owned) and kill
  simulator.py                `simulator` controls: action whitelist, read-back, pin snapshots
  ios_prefs.py                the shut-down simulator's preference store (`keyboard pin`)
  device_state.py             deep links, permissions, location, media, crashes, files, recording
  screenshot.py / logs.py     per-platform dispatch; shots carry provenance metadata in the PNG
  follow.py                   bounded NDJSON follows: file tail, device-log stream, store poll
  metrics/
    meminfo.py                dumpsys meminfo / proc status / cpuinfo parsers
    process.py                pid resolution with sources_tried on failure
    snapshot.py               per-platform load summary: Android CPU from two /proc reads
                              (one-core scale, cpuinfo fallback marked stale); iOS is host
                              accounting, and says so
    series.py                 first/last/delta/slope math; directional leads, never "leak"
    presets.py                which heavy profilers this host can run
    android_memory.py         evidence pack: metadata+meminfo+proc+gfxinfo+HPROF, analyze
    frames.py                 gfxinfo reset/capture (best-effort parse; 0 frames carry no
                              percentiles) + Flutter timings
    trace.py                  simpleperf (--app, PMU-aware event) / gfxinfo-flow / xctrace
                              presets → artifacts
  processes.py                machine-wide process registry (proxies, log streams, canvas
                              pairs, idb companions), signature-checked reaping, group
                              remnants reported never signalled, command-line redaction
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
    attachment.py             `network status` evidence: only a flow whose client is the
                              target proves attachment
```

Two rules carry the design.

1. **Actuation is single-sourced per backend.** Every idb command line lives in
   `ios_idb.py`, every adb command line in `adb.py`, every simctl command line
   in `ios_simctl.py`, so an Xcode upgrade that breaks idb has a one-file blast
   radius. The one AXe command line lives in `ui_ios.py` beside the routing
   decision. `doctor.py` is the deliberate exception: it probes `idb
   list-targets` and SimulatorKit's location itself, because a diagnostic must
   keep working when the wrapper does not. The Mobile Canvas scrcpy transport
   is the other one: its Node process writes streamed input to scrcpy's
   control socket itself and only journals through the Python bridge (see
   [Mobile Canvas](#mobile-canvas)).
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
| `ui tree\|find\|wait\|tap\|swipe\|type\|key` | understand and control the screen; `ui pinch\|rotate\|shake` exist but have no backend on either platform and are refused |
| `screenshot`, `shots list\|show`, `record start\|stop` | visual evidence and its provenance |
| `note add\|list`, `journal` | the run's own record: what was done, what was concluded |
| `logs tail`, `crash list\|show` | textual evidence |
| `open`, `permissions`, `location`, `media`, `file` | drive device state |
| `simulator *` | battery, network, push, telephony, biometric, appearance, text size, clipboard, status-bar/keyboard/animation pins; `verified` only after a read-back |
| `network *` | consent-gated HTTP(S) capture, mock, HAR |
| `atlas update\|show\|coverage\|paths\|export\|diff` | the observed application graph, evidence-linked |
| `proof --base` | run the covering flow suite for a diff; pass/fail/not_covered/blocked/inconclusive |
| `report build\|open\|export\|suite` | addressable per-step evidence UI, self-contained HTML, and JUnit |
| `report serve` | loopback-only, token-protected replay-to-step controls over recorded runs |
| `flow check\|fmt\|list\|run` | validate, canonicalize, enumerate, and execute Flow v1 files (`docs/FLOW.md`); `run` polls assertions, fires mutations once, and supports prefix replay |
| `processes`, `cleanup` | machine-wide reaping of what Autonom started |
| `session outputs`, `logs follow`, `network requests follow`, `journal --follow` | live session watch: stream catalog + bounded NDJSON follows |
| `metrics *` | snapshot/series, memory pack, frames, trace presets; honest per-platform semantics (`docs/plans/PHASE_4_METRICS.md`) |

Every verb except `note`, `journal`, and `version` passes through one journal
choke point in `main()`, so the timeline records the failures too — including
the ones the agent chose not to mention.

### iOS input backends

The accessibility tree always comes from idb (`describe-all`). Input — tap,
swipe, type, key, long press, double tap — is routed per call by
`ui_ios.hid_backend()`:

| `--ios-hid` / `AUTONOM_IOS_HID` | Route |
| --- | --- |
| `idb` | idb only; a HID failure is reported, never retried elsewhere |
| `axe` | AXe only (`--axe PATH` / `AUTONOM_AXE`, else `axe` on `PATH`); no binary is an error |
| `auto` (default) | idb, unless idb's HID is known broken and AXe is installed |

"Known broken" is either the doctor probe (a PATH-resolved idb whose
companion cannot load SimulatorKit — Xcode 27 moved the framework to
`Contents/SharedFrameworks`, companions built before the fix look in
`Library/PrivateFrameworks`) or idb itself answering
`ios_hid_framework_missing`. That failure happens before any event is
delivered, so re-sending the same input through AXe repeats nothing. The
probe runs once per process. Every input payload names the `backend` it
used, so evidence says which tool touched the screen. The idb retry for a
stale companion follows the same rule: at most one retry, and only for a
refused connection, which never delivered the action.

### Journal redaction

The journal is written from `argv`, so it must know which tokens are
secrets. Guessing from strings alone failed repeatedly (argparse accepts any
unambiguous abbreviation, `--flag=value`, and flags on either side of the
verb), so `main()` hands the journal the real parser and the parsed
namespace. `journal.canonical_argv()` resolves every token the way argparse
would — canonical option, its dest, how many values it takes — reading the
parser's option tables without parsing anything. Three scrubs then run on
the same argv, each strictly one output token per input token:

1. **parser-resolved** — a value is secret when its canonical option, its
   dest, or the spelling as typed is secret-bearing; every other value gets
   content scrubbing (`password=`, `?token=`, JSON fields);
2. **argv-only** — the conservative prefix scan, used alone when the parser
   cannot resolve the argv;
3. **the previous release's exact rule**, vendored verbatim.

`scrub_for_journal()` writes, per token, the most masked of the three, so
the journal is never less masked than any of them by construction. The
price is over-masking (every `KEY=VALUE` option value is journaled as
`KEY=<redacted>`); a differential test against the vendored rule guards the
superset property.

### Pin snapshots

A pin changes device state a person may have set on purpose (a battery
override for a low-battery test, a custom animation scale). The first
`status-bar pin` or `animations pin` records what it is about to replace
under `$AUTONOM_HOME/simulator-state/<target>.json` (mode `0600`); a second
pin merges only keys not yet recorded, so the snapshot always describes the
device before the **first** pin, and `clear`/`reset` restores exactly that
and deletes the section. `keyboard pin` keeps its own per-UDID snapshot under
`simulator-prefs/` with the same merge rule.

## Mobile Canvas

`canvas serve` supervises one Node process that serves the page, the device
picture and its input on `127.0.0.1`. It lives with its skill in
`plugins/autonom/skills/android-emulator-browser/scripts/` and has no npm
dependency: the WebSocket server and the scrcpy wire format are written by
hand, and the encoders are tested against the byte arrays of scrcpy's own
serialization tests.

| File | Role |
| --- | --- |
| `android-emulator-browser.mjs` | HTTP and WebSocket endpoints, auth, transport choice, video fan-out, handoff, the journal queue, the page |
| `browser-lib.mjs` | argument parsing, the Host and Origin checks, strict control-message validation |
| `scrcpy-lib.mjs` | the scrcpy 4.1 protocol without I/O: video headers, control encoders, device messages, the codec string, the browser key map |
| `scrcpy-session.mjs` | one scrcpy-server on the device (push, `adb forward`, start, sockets, restart, stop) and server discovery |
| `ws.mjs` | the server side of RFC 6455 |
| `scripts/autonom_canvas_bridge.py` | the persistent Python bridge: HTTP input through `ui.*`, and the `record` operation, which only journals |
| `canvas-devices.mjs` | device ids, `/d/<id>/` paths, the registry of attached devices and their states, focus profiles |
| `canvas-workspace.mjs` | tabs (1-4 tiles each), placement, the saved workspace file and its lock |
| `canvas-sessions.mjs` | the automatic Autonom session per attached device (start `--alongside`, reuse, release) |
| `canvas-workspace-page.mjs` | the workspace page: tab bar, tiles (each an embedded device page), picker |
| `canvas-actions-page.mjs`, `canvas-activity.mjs` | the Actions drawer of a device page; the command log and `/api/activity`, `/api/health` |

```text
browser page
  ├─ /ws/video ◀─────────── H.264 packets ──┐
  ├─ /ws/control ────────── JSON input ────▶│
  ├─ /stream.mjpeg ◀─────── frames ─────────│  Canvas (node)
  └─ /tap /swipe /key /text ───────────────▶│
                                            ├── adb forward ──▶ scrcpy-server 4.1 on the device
                                            ├── one capture loop: screenrecord + ffmpeg, or screencap
                                            └── NDJSON ──▶ autonom_canvas_bridge.py ──▶ ui.* actuators, journal
```

### Transport choice

`auto` on Android takes the first that works: scrcpy when a scrcpy-server
4.1 resolves, `screenrecord` + ffmpeg when the device produces H.264, then
`screencap`. The server comes from `--scrcpy-server` (with `--scrcpy-version`
when its file name does not say), `AUTONOM_SCRCPY_SERVER`,
`SCRCPY_SERVER_PATH`, then the server an installed `scrcpy` ships beside its
binary, found through the symlink to its real prefix and versioned by
`scrcpy --version`. The first source that is set decides: a broken one is
reported, never skipped, so the Canvas never quietly uses another file than
the one configured. Any version but 4.1 is refused, because scrcpy keeps no
protocol compatibility between releases, and Autonom never downloads a
server. `doctor.py` repeats the same resolution for `optional_tools.scrcpy`.

H.264 output from `screenrecord` is probed by running `screenrecord
--output-format=h264 --time-limit 1` for at most 5 s and looking for an SPS:
Android 16 (API 36) no longer lists that hidden option in `--help`, although
it still works. `fallback_reason` in `/status` and in the startup output names
every skipped step. A scrcpy server that never streams under `auto` (a device
that cannot encode H.264, say) moves the Canvas to the multipart fallback at
run time; an explicit `--transport scrcpy` keeps retrying instead.

### Lifecycle and video

One device server per Canvas. It starts with the first WebSocket client,
stops 15 s after the last one leaves, and is stopped on shutdown, which waits
up to 3 s for it. The server file is pushed to
`/data/local/tmp/autonom-scrcpy-4.1.jar` and started through `adb shell
app_process` with `tunnel_forward=true audio=false control=true cleanup=true
video_codec=h264 clipboard_autosync=false` plus the size, bit-rate and fps
caps. States run idle → starting → streaming ↔ restarting → stopped. An
unexpected exit restarts after 1 s, doubling to 10 s and back to 1 s after
30 s of streaming; a stopped session never restarts. Stopping closes both
sockets, ends the `adb shell` child, removes the `adb forward`, and kills a
server still waiting for its sockets by its `scid`.

`/ws/video` carries binary messages — `1` session (u32 width, u32 height),
`2` codec config (SPS/PPS), `3` packet (a flag byte whose bit 0 marks a key
frame, u64 PTS in µs, the Annex B payload) — with each payload byte for byte
as the server sent it, plus JSON `state` messages. A late joiner gets the
session, the config and the packets since the last key frame. That cache is
capped at 8 MiB; past it the cache is dropped and a key frame requested. A
client more than 4 MiB behind gets no packets until the next key frame, which
is requested for it (`RESET_VIDEO`, at most once a second), so one slow tab
never holds the others back. Session and config messages come with every
reset and rotation, so a client dropping packets, or 64 KiB behind, keeps only
the latest of each, and gets them, session first, before its next packet. The page decodes with WebCodecs and falls back
to `/stream.mjpeg`, with its reason, when it cannot. Multipart clients of
every transport, iOS included, share one capture loop, and a client more
than 2 MiB behind skips frames.

### Input and the journal

Streamed input is the one place the Canvas actuates without the bridge,
because a process hop per pointer move is too slow. Each message is
validated (finite numbers, ranges, enums, the key allowlist, at most 64 KiB),
checked against the handoff state, and written to the control socket in
arrival order. Coordinates arrive normalized and are mapped to the video size
current when the message is handled. scrcpy ignores a positional event made
for another size than its current one, and the Canvas learns of a rotation
only from the next session header, so when that header brings a new size the
Canvas lifts every pointer still down with the new size (their gestures end,
and later moves for them are refused) and sends again, with the new size, the
up of any pointer released in the second before. Device pointer ids 0..9 are
unique across connections. Keycodes stand for US key positions, so the page
(`keyInputFor` in `scrcpy-lib.mjs`) sends a printable key as text when its
character differs from what a US layout types there, Shift considered, or its
position has no keycode, unless Meta or a plain Ctrl makes it a shortcut:
other layouts, the AltGr layer (Ctrl+Alt or the AltGraph modifier) and the
macOS Option layer (Alt alone) type their own characters. Ctrl+V and Cmd+V
send nothing, so the browser's paste event carries the clipboard. A dead key,
and a keydown that only feeds an input method while it composes, sends
nothing; the composed character comes with the next keydown. Composition beyond dead keys
(CJK input methods) is not handled on the screen; the text box takes it.
Printable ASCII is typed with `INJECT_TEXT`; any other text, and every paste,
goes through `SET_CLIPBOARD` with paste, which replaces the device clipboard.
scrcpy-server sets the clipboard and only injects the paste key, and the app
reads the clipboard when it handles that key, so a later `SET_CLIPBOARD` or
key could overtake it. Each such write therefore carries a non-zero sequence,
and no other device input is written until the device's `ACK_CLIPBOARD` for
it plus a 200 ms settle delay, or 1 s without an ack (noted in `last_error`).
The settle delay is a heuristic: an app that handles the paste key later than
that can still read a newer clipboard and lose text in a fast burst of
Unicode text interleaved with other input.
Messages that arrive meanwhile, on any connection, wait as held input and are
handled in arrival order, with their handoff checks and journal records at
that time. A connection is still read while only this wait holds it, up to
64 KiB, then paused like for any other bound. A pointer or key lifted
meanwhile waits behind the paste too. A server restart or stop drops the
wait: no ack will come, and held input meets the session check and is refused.
A paste takes effect only if the focused app handles `KEYCODE_PASTE` (Android
7 and later); in an app that ignores it the text never arrives, while the
journal records that the text was sent, not that the app accepted it.

A pause, a takeover by another origin, a disconnect, a refusal or a server
restart lifts every pointer and key the connection holds. What a dead server
injected stays down on Android, so on a restart those pointers and keys are
remembered (their device pointer ids stay taken) and lifted through the new
server once its first session header gives the video size. So is every up
and key-up the dead server may never have read: one still waiting behind a
paste, or written to it in the last second. The Canvas sends
one `record` per completed action — gesture, wheel burst, key press, text
entry, paste, system button, control change — to the bridge, which rebuilds
the summary from an allowlist (never clipboard text, never sensitive text),
writes the `canvas-<kind>` action detail and the `ui <kind>` journal entry
with the action's origin, and never calls an actuator. A pointer or key the
Canvas lifted is journaled like one its client released.

The bridge journals into the current Autonom session only when that session
is on the Canvas's own target (same `target_id`, and same `platform` when the
record names one). With no current session, or one on another device, the
action still runs and answers ok, and nothing is journaled, so a Canvas on
one device never writes into the record of a session on another. The rule is
the same for `record` and for the HTTP input the bridge actuates (`tap`,
`swipe`, `key`, `text`).

### Backpressure

Nothing a client sends is buffered without bound. A control connection stops
being read while the device has not read 256 KiB of control bytes, while the
connection has not read 256 KiB of replies, while 64 of its journal records
are unanswered, or while 256 records wait in all; it is read again once each
is back under half its bound. Messages already parsed from the read that
paused it wait in order, up to 1 MiB; past that the connection is closed with
1013. A client with more than 4 MiB of unread replies is closed with 1008,
and one more than 64 KiB behind gets the current state once it catches up
instead of every state message. Journal records go to the bridge in order,
16 at a time; `/status` reports `scrcpy.journal_pending`, and a record
produced while 256 are already pending (a pointer lifted by a takeover during
a flood) is dropped and counted in `scrcpy.journal_dropped`. Bounded memory
is the promise, not throughput: a client that already holds the token or the
cookie can still make input slow, delay journaling, or get itself closed.
That risk is accepted, because such a client can drive the device anyway.

### Canvas workspace

`autonom canvas` (or `canvas serve` with no target or device flag) starts a
**workspace Canvas**: one Node server with no device, registered with no
session owner, open until Ctrl+C or `autonom canvas stop`. Its main()
starts `startCanvas(options, hooks)` with the workspace page, the Actions
drawer as a page extension of every device page, the activity and health API
routes and one command log; the server itself works without them (tests use
its built-in page).

```text
workspace page /c/<tab>  ──▶ /api/workspace, /api/devices, /api/targets, /api/boot (polling, POST)
  └─ tile iframe ─▶ /d/<device id>/?embed=1  ──▶ that device's context: transport, bridge, tools, session
autonom canvas stop|attach|detach|list ──▶ <state>/canvas/<port>.json ──▶ /api/* with the Bearer token
```

Each attached device gets its own context (the single-device Canvas's state,
bridge and tools process), its own `/d/<id>/` routes and its own Autonom
session: the server runs `session start --alongside --started-by
canvas:<port>:<pid>` for it, or reuses the live session already on that
target, and never writes `current.json`. Commands with `--serial X` bind to
the session on X, so an agent's commands journal next to the Canvas's. A
device lives in one tab; tabs persist in `<state>/canvas/workspaces/<name>.json`
and one lock file keeps a workspace to one Canvas. Only the focused tile
streams at full rate; a crash inside one device's scope restores that device
alone. With `--serial`, `--udid`, `--target` or one `--device` the Canvas is
the single-device Canvas of before, byte for byte.

### Canvas security

Every HTTP request and WebSocket upgrade must name the Canvas in its `Host`
header (`127.0.0.1:<port>` or `localhost:<port>`), which defeats DNS
rebinding. Browsers apply no CORS to WebSockets, so an upgrade from the page
needs the HttpOnly `SameSite=Strict` session cookie, that session's CSRF
value in the `csrf` query, and a Canvas `Origin`; API clients use the token
(query or bearer) and may name their origin. A foreign `Origin` is refused
whatever it carries. Another local port is the same site and shares the
cookie, so the page refuses to be framed (`frame-ancestors 'none'`,
`X-Frame-Options: DENY`, and a 403 for framed loads), and a reloaded page
gets its CSRF value back only from a same-origin fetch.

## Compact node schema

The single most important contract: an iOS node and an Android node are
indistinguishable in shape, which is what lets one skill body drive both.

```json
{"ref": "n5", "role": "button", "text": null, "desc": "General",
 "resource_id": "com.apple.settings.general", "class": "Button", "package": null,
 "bounds": [16, 380, 386, 432], "clickable": true, "long_clickable": false,
 "checkable": false, "enabled": true, "focusable": false, "focused": false,
 "scrollable": false, "selected": false, "checked": false, "depth": 0}
```

`long_clickable` and `checkable` are UI Automator's attributes on Android; on iOS
`checkable` marks a toggle (whose `checked` comes from `AXValue`) and
`long_clickable` is always false, because accessibility has no long-press trait.
Live trees add `parent`, and a node that cannot be on screen carries
`visible: false` — iOS lists Flutter's scroll cache at a 0x0 frame, which a tap
would have sent to the screen's origin. Such nodes stay in the tree (they are
counted), `ui tap` refuses them with `element_offscreen`, `ui find` and `ui tap`
resolve on-screen matches first, and a flow never selects them.

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
- **Fake backends** — `tests/fakes/fake_{adb,simctl,idb,axe}.py` record their argv, so
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
- **Canvas against a fake device** — `tests/browser-scrcpy.test.mjs` runs
  the real Canvas process against a fake adb, an in-process fake scrcpy
  device behind the forward port, and a fake journal bridge: discovery, auth,
  fan-out, input, journal, and a memory-bound flood test per message type.
  `tests/scrcpy-lib.test.mjs` compares every encoder with the bytes of
  scrcpy's own serialization tests.
- **Device-backed** — CI runs no emulator or simulator; device runs are
  manual and evidenced by before/after artifacts, never by exit codes alone.
  `tests/live/canvas_scrcpy_live.mjs` (cases `picture`, `bench`, `tabs`,
  `restart`, `input`, `journal`) is such a run for the Canvas: it needs an
  explicit `--serial`, runs in its own temporary `AUTONOM_HOME` so it never
  reads, stops or writes the operator's sessions, writes one JSON report per
  case into `--evidence-dir`, and is never run by `run_checks.sh`.

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
must never be exposed publicly. The Canvas also refuses any request whose
`Host` header is not its own, accepts a WebSocket only with the session
cookie, its CSRF value and its own `Origin` (or the token), and cannot be
framed ([Canvas security](#canvas-security)). Consent for MITM and CA
installation is required per invocation: a flag plus a typed phrase on a
terminal, never cached, never grantable by an environment variable or a prior
run. App-container file access is
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

### Optional XCUITest backend

`ios_xctest.py` owns a targetless, cached UI-test runner from `native/ios`.
`ui_ios.py` selects it after unusable idb snapshots (or when `--ui-backend
xcuitest` asks); otherwise idb keeps the tree and the idb/AXe HID route keeps
the input. `ios_geometry.py` can measure Simulator points independently of
accessibility. A nonce-bound,
single-dispatch mailbox stays inside the selected Simulator container.
Commands and flow events keep backend provenance. Explicit `--session-id`
binds state without changing the global pointer. See [UI recovery](IOS_UI_RECOVERY.md).
