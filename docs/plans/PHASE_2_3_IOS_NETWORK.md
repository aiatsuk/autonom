# Deep plan: Phase 2 (iOS) + Phase 3 (Network)

Status: planning only (no implementation in this doc).  
Depends on: Phase 1 Android CLI (`scripts/autonom.py`, compact UI schema, `.autonom/` sessions).  
Target versions: **0.5.0** (Phase 2), **0.6.0** (Phase 3).

---

## 0. Shared principles (both phases)

### 0.1 Contract stability

Phase 1 established agent-facing JSON. Phases 2–3 **extend** verbs; they must not break:

| Stable field | Rule |
| --- | --- |
| `{ "ok": true/false, ... }` | Top-level success envelope |
| Compact UI node | `ref`, `role`, `text`, `desc`, `bounds`, `clickable`, `enabled`, … |
| Session record | `session_id`, `platform`, `serial` (or `udid`), `app_id`, `artifacts_dir` |
| Errors | stderr JSON `{"ok":false,"error":"..."}` exit 2 |

**Platform-neutral target identity:**

```json
{
  "platform": "android" | "ios",
  "target_id": "emulator-5554" | "UDID-or-simulator-UDID",
  "aliases": { "serial": "...", "udid": "..." }
}
```

CLI flags:

- `--platform android|ios|auto` (default `auto` when session exists)
- `--serial` kept for Android; accepted as alias of `--target` / `--udid` on iOS
- Prefer new flag `--target <id>` everywhere; map old `--serial` → target_id

### 0.2 Backend dispatch

```text
autonom_lib/
  platform.py          # resolve platform + target from flags/session
  adb.py               # Android (exists)
  ios_simctl.py        # NEW: simctl wrapper
  ios_idb.py           # NEW: idb wrapper
  ui.py                # dispatch + compact schema (extend)
  ui_android.py        # extract current UIAutomator path
  ui_ios.py            # idb describe-all → compact nodes
  screenshot.py        # dispatch
  logs.py              # dispatch
  session.py           # platform-aware
  network/             # Phase 3
    proxy.py
    store.py
    mitm_addon.py
    device_proxy_android.py
    device_proxy_ios.py
```

Skills stay thin: call CLI only; no platform branches in SKILL.md beyond examples.

### 0.3 Host constraints

| Constraint | Implication |
| --- | --- |
| iOS Simulator requires **macOS + Xcode** | Linux CI: fixture/fake-simctl only; mark real iOS tests `macOS` |
| idb companion install friction | `autonom doctor` explains; soft-fail with actionable error |
| Network MITM is privileged | Explicit confirm / `--i-understand-mitm`; never silent CA install |
| Certificate pinning | Document “debug builds only”; do not claim store-app MITM |

### 0.4 Ordering recommendation

**Implement Phase 2 fully before Phase 3**, then network on Android first, then iOS network.

Rationale: Phase 3 needs a stable multi-platform session + target model; doing network only on Android first still requires the session fields Phase 2 introduces (`platform`, `target_id`). Minimal shared refactor can land as **2.0 prelude** (1–2 days) before iOS UI work.

```text
2.0  Platform abstraction + devices list (android+ios) + session fields
2.1  iOS session lifecycle (boot/install/launch)
2.2  iOS UI tree/find/tap/type/key + screenshot + logs
2.3  Skills + doctor + tests + docs  → tag 0.5.0
3.0  Network core (mitm store + CLI) offline/fixture tests
3.1  Android device proxy attach
3.2  Mock rules + HAR export
3.3  iOS simulator proxy attach
3.4  Skills + security docs + tests  → tag 0.6.0
```

---

## Phase 2 — iOS simulator parity

### 2.1 Goals

Agent can run the **same mental model** as Android:

1. List iOS simulators (+ Android devices in one list).
2. Start session with `platform=ios`, boot simulator if needed.
3. Install `.app` / launch by bundle id.
4. Dump compact UI tree; find/tap by text or accessibility label.
5. Screenshot + recent logs.
6. Stop session; artifacts under `.autonom/<id>/`.

**Non-goals for 0.5.0**

- Physical iPhone (Developer Mode, WDA signing, tunnels) — Phase 2.x later.
- tvOS / watchOS.
- Full XCUITest project generation.
- Replacing Maestro / Appium for complex multi-app flows.
- iOS browser MJPEG mirror (optional stretch; not exit criterion).

### 2.2 Backend stack

| Concern | Tool | Why |
| --- | --- | --- |
| Device lifecycle | `xcrun simctl` | First-party, scriptable |
| Install / launch / terminate | `simctl install`, `simctl launch`, `simctl terminate` | Reliable on Simulator |
| Screenshot | `simctl io <udid> screenshot` | Simple PNG |
| Record (optional) | `simctl io <udid> recordVideo` | Nice-to-have in 2.2 |
| Accessibility tree + gestures | **Facebook idb** (`idb ui describe-all`, `tap`, `text`, `button`) | Agent standard; a11y-first |
| Logs | `xcrun simctl spawn <udid> log stream` or `log show --predicate` | Filter by subsystem/process |

**Prerequisites (document in doctor):**

```bash
xcode-select -p
xcrun simctl list devices available
# idb
brew tap facebook/fb
brew install idb-companion
pipx install fb-idb   # or project-documented equivalent
idb list-targets
```

If idb missing: session/screenshot/logs may still work via simctl; `ui *` returns clear error `idb_required`.

### 2.3 CLI surface (Phase 2)

#### devices (unified)

```bash
python3 scripts/autonom.py devices
python3 scripts/autonom.py devices --platform ios
python3 scripts/autonom.py devices --platform android
```

Response:

```json
{
  "ok": true,
  "devices": [
    {
      "platform": "android",
      "target_id": "emulator-5554",
      "state": "device",
      "name": "sdk_gphone64_arm64",
      "properties": {}
    },
    {
      "platform": "ios",
      "target_id": "A1B2-UDID",
      "state": "Shutdown" | "Booted",
      "name": "iPhone 16",
      "runtime": "iOS 18.2",
      "properties": { "is_available": true }
    }
  ]
}
```

#### session

```bash
# Boot if needed + start artifacts
python3 scripts/autonom.py session start \
  --platform ios \
  --target A1B2-UDID \
  --app-id com.example.app

# Install .app from DerivedData / build output
python3 scripts/autonom.py session start \
  --platform ios --target A1B2-UDID \
  --install /path/to/MyApp.app \
  --launch --app-id com.example.app

python3 scripts/autonom.py session launch com.example.app
python3 scripts/autonom.py session force-stop com.example.app
python3 scripts/autonom.py session clear com.example.app   # simctl privacy / uninstall+install? see below
python3 scripts/autonom.py session stop
```

**`session clear` on iOS:** there is no perfect `pm clear`. Options (pick one primary, document others):

| Strategy | Command-ish | Use when |
| --- | --- | --- |
| **A (default)** | `simctl uninstall` + `simctl install` | Full reset; needs install path stored in session |
| **B** | `simctl privacy <udid> reset all <bundle>` | Permissions only |
| **C** | erase keychain container via `simctl get_app_container` + rm (fragile) | Avoid as default |

Recommend **A** when `--install` path known; else return error with guidance.

#### ui (same verbs)

```bash
python3 scripts/autonom.py --platform ios --target UDID ui tree
python3 scripts/autonom.py ui find --text "Log In" --mode contains
python3 scripts/autonom.py ui tap --text "Continue"
python3 scripts/autonom.py ui tap --x 200 --y 400
python3 scripts/autonom.py ui type "hello"
python3 scripts/autonom.py ui key HOME|LOCK|SIRI|SIDE_BUTTON   # idb buttons; map Android KEYCODE_* separately
```

#### screenshot / logs

```bash
python3 scripts/autonom.py screenshot --out /tmp/ios.png
python3 scripts/autonom.py logs tail --package com.example.app --since 60 --max-lines 200
```

### 2.4 iOS UI tree → compact schema

idb `ui describe-all` returns nested accessibility elements (AX frames, labels, values, traits). Map to Phase 1 compact nodes:

| Compact field | iOS source |
| --- | --- |
| `ref` | stable index `n{i}` in depth-first order (same as Android) |
| `role` | normalize AX trait/type → `button`, `text`, `textfield`, `image`, `cell`, … |
| `text` | `AXValue` or title if text-like |
| `desc` | `AXLabel` / accessibility label |
| `resource_id` | null on iOS (or bundle-relative identity if available) |
| `class` | raw AX type string |
| `package` | foreground bundle id if known |
| `bounds` | `[x, y, x+w, y+h]` from frame (points; document scale) |
| `clickable` | traits include button / allows hit testing |
| `enabled` | enabled flag |
| `focusable` | best-effort from traits |

**Coordinate space:** Simulator points vs pixels (Retina). idb tap typically uses points matching describe-all frames — **do not** double-scale. Document in skill; store `display.scale` in session when obtainable.

**Meaningful-only filter:** keep nodes with label/value/button trait/text field; drop empty generic containers (same token budget goal as Android).

**Find modes:** exact / contains / regex on `text` and `desc` (label). No `resource_id` on iOS → error if only resource_id selector supplied without other fields.

**Duplicates:** same as Android — require `--index` when multiple matches.

### 2.5 idb connection model

1. Resolve simulator UDID.
2. Ensure Booted (`simctl boot` + `bootstatus -b`).
3. `idb connect <udid>` or `idb list-targets` and select.
4. Cache companion readiness in session metadata: `{"idb": "ready"|"missing"|"error"}`.
5. All UI ops check cache; refresh on failure once.

Timeouts: boot up to 120s; ui describe 20s; tap 10s.

### 2.6 Logs on iOS

Preferred approach for agents (finite buffer, JSON lines):

```bash
# Conceptual
simctl spawn <udid> log stream --style compact --level info
# with predicate:
# eventSender == "com.example.app" OR processImagePath CONTAINS "MyApp"
```

Phase 2.0 implementation strategy:

1. Start `log stream` in background to `artifacts/logs/stream.log` at session start when `--app-id` set (optional `--log-stream`).
2. `logs tail` reads the last N lines and/or runs `log show --last <since>s` if available.

Filtering is best-effort; never block UI ops on log daemon failures.

### 2.7 Skills (Phase 2)

| Skill | Change |
| --- | --- |
| `mobile-session` | Dual-platform examples; boot rules; clear semantics on iOS |
| `mobile-screen` | idb prerequisites; label vs resource-id; coordinate space |
| `project-router` | Detect `.xcodeproj`, `.xcworkspace`, `Package.swift`, `ios/` in Flutter; load iOS runtime skills |
| `ios-debugger-agent` (new) | Build/run via xcodebuild/flutter, attach session, evidence ladder for iOS |
| `ios-project-setup` (new, lighter) | Optional: scheme/simulator selection conventions — can slip to 0.5.1 if timeboxed |

Keep code-architecture SwiftUI skills for Phase 6 unless free bandwidth.

### 2.8 Testing strategy (Phase 2)

| Layer | What | CI |
| --- | --- | --- |
| Unit | parse fixture JSON from idb describe-all → compact nodes | Linux+macOS |
| Unit | find/tap selection logic platform-agnostic | Linux+macOS |
| Fake | fake-simctl + fake-idb scripts (like fake-adb) for session/ui | Linux+macOS |
| Integration | real Simulator smoke (boot, tree, screenshot) | **macOS only**, optional/manual or tagged |

Fixtures to add:

- `tests/fixtures/idb_describe_all_sample.json`
- `tests/fixtures/simctl_list_devices.json`

### 2.9 Exit criteria (Phase 2 → 0.5.0)

- [ ] `devices` returns booted/shutdown simulators on a Mac with Xcode.
- [ ] `session start --platform ios --target <udid>` creates `.autonom` artifacts with `platform: ios`.
- [ ] Install `.app` + launch bundle id works on Simulator.
- [ ] `ui tree` returns compact nodes from idb (or documented skip if idb absent).
- [ ] `ui find` / `ui tap --text` works on a sample system or demo app.
- [ ] `screenshot` writes PNG.
- [ ] `logs tail` returns non-empty structure (may be empty lines but ok envelope).
- [ ] Fake-backend unit tests pass on Linux CI.
- [ ] Docs: INSTALL prerequisites, CAPABILITIES matrix updated, USAGE iOS prompts.
- [ ] Android Phase 1 regression still green.

### 2.10 Risks (Phase 2)

| Risk | Mitigation |
| --- | --- |
| idb install/API drift | Pin documented versions; isolate in `ios_idb.py`; fixture-based tests |
| describe-all huge trees | meaningful_only + max_nodes + max_depth |
| Flutter iOS semantics sparse | Skill guidance: add semantics; fall back to screenshot + coordinates |
| Boot flakiness | `bootstatus -b`, retry once, clear error |
| Apple tools only on macOS | Fake backends for CI; document Mac requirement for real runs |

### 2.11 Effort estimate

| Slice | Effort |
| --- | --- |
| 2.0 platform abstraction | 1–2 days |
| 2.1 session lifecycle | 1–2 days |
| 2.2 UI + screenshot + logs | 3–5 days |
| 2.3 skills/docs/tests | 1–2 days |
| **Total** | **~1.5–2.5 weeks** calendar for one engineer |

---

## Phase 3 — Network inspect + mock

### 3.1 Goals

Agent can:

1. Start a **localhost MITM proxy** bound to the Autonom session.
2. Attach the **current device** (Android emulator first, iOS simulator second) to that proxy.
3. **List/filter** captured HTTP(S) requests as JSON.
4. **Mock** responses by URL/method match (status, headers, body file).
5. Export **HAR** (or HAR-like JSON) into session artifacts.
6. Stop cleanly (remove device proxy settings when possible; tear down mitm).

**Non-goals for 0.6.0**

- Transparent MITM of production apps with cert pinning.
- Full GUI (Proxyman/Charles replacement).
- HTTP/2 multiplex debugging edge cases perfection.
- Cloud device farm proxy routing.
- WebSocket mock language (inspect only or pass-through first).

### 3.2 Why mitmproxy

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| **mitmproxy** | OSS, Python, addons, scriptable, headless `mitmdump` | CA install ceremony | **Primary** |
| HTTP Toolkit | Excellent Android UX | Desktop app; hard to embed as library | Document interop only |
| Proxyman | Great macOS iOS | Proprietary, GUI | Document interop only |
| okhttp mock web server | In-process app-only | Requires app test hooks | Future app-side skill |

### 3.3 Architecture

```text
                    ┌─────────────────────────┐
  Device traffic ──►│ mitmdump :PORT          │
  (emulator/sim)    │  autonom mitm addon     │
                    │  - record flows         │
                    │  - apply mock rules     │
                    └───────────┬─────────────┘
                                │ IPC
                    ┌───────────▼─────────────┐
                    │ Flow store              │
                    │  .autonom/s_…/network/  │
                    │  flows.jsonl            │
                    │  mocks.json             │
                    │  mitm-ca/               │
                    │  proxy.pid              │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │ autonom network * CLI   │
                    └─────────────────────────┘
```

**IPC options (choose one in 3.0 design spike):**

| Option | Mechanism | Tradeoff |
| --- | --- | --- |
| **A. JSONL file sink** | Addon appends each flow; CLI tails/filters file | Simple, crash-safe, easy tests |
| **B. Local HTTP control API** | Addon serves `127.0.0.1:PORT+1` control | Faster mock updates; more code |
| **C. mitmproxy client API** | Connect to running instance | Version coupling |

**Recommend A + small control socket for mock reload (hybrid):** flows as JSONL; `mocks.json` watched/reloaded by addon each request or on SIGHUP.

### 3.4 CLI surface (Phase 3)

```bash
# Start proxy for current session (or create network-only session)
python3 scripts/autonom.py network start [--port 8080] [--platform android|ios]

# Device attach (sets proxy + ensures CA path documented/automated)
python3 scripts/autonom.py network attach
python3 scripts/autonom.py network detach

# Introspection
python3 scripts/autonom.py network status
python3 scripts/autonom.py network requests list \
  --host api.example.com \
  --method POST \
  --status 401 \
  --since 60 \
  --max 50
python3 scripts/autonom.py network requests show <id>
python3 scripts/autonom.py network export --har network/session.har

# Mocking
python3 scripts/autonom.py network mock add \
  --match '*/v1/login' \
  --method POST \
  --status 500 \
  --body-file fixtures/login_error.json \
  --header 'Content-Type: application/json'
python3 scripts/autonom.py network mock list
python3 scripts/autonom.py network mock remove <mock_id>
python3 scripts/autonom.py network mock clear

# Tear down
python3 scripts/autonom.py network stop
```

#### Request list item schema

```json
{
  "id": "f_00a1",
  "started_at": "2026-…",
  "method": "POST",
  "url": "https://api.example.com/v1/login",
  "host": "api.example.com",
  "path": "/v1/login",
  "status": 500,
  "duration_ms": 42,
  "request_headers_preview": { "content-type": "application/json" },
  "response_headers_preview": {},
  "request_body_preview": "{\"email\":\"…\"}",
  "response_body_preview": "{\"error\":\"…\"}",
  "mocked": true,
  "mock_id": "m_12",
  "sizes": { "request_bytes": 120, "response_bytes": 80 }
}
```

**Redaction (hard requirement):**

- Auto-redact headers: `authorization`, `cookie`, `set-cookie`, `x-api-key`.
- Body preview truncated (e.g. 2 KiB); full body only on `requests show --full` with warning.
- Never print into agent-facing default list more than previews.

#### Mock rule schema

```json
{
  "id": "m_12",
  "match": {
    "url_glob": "*/v1/login",
    "method": "POST",
    "host": null
  },
  "response": {
    "status": 500,
    "headers": { "content-type": "application/json" },
    "body_path": "network/mocks/m_12.body"
  },
  "enabled": true
}
```

Match order: first enabled rule wins. Support glob (`fnmatch`) and optional regex (`--match-regex`) later if needed.

### 3.5 Device attach procedures

#### Android emulator (3.1 — first)

1. Start mitmdump with Autonom addon; write CA to `network/mitm-ca/mitmproxy-ca-cert.cer`.
2. Install CA on emulator:
   - **Debug user CA:** `adb push` + settings (limited on API 24+ for apps targeting modern SDK).
   - **Preferred for emulators:** Google APIs image + `adb root` + install as **system** CA (documented script), OR instruct app `network_security_config` trust user CAs for debug.
3. Set proxy: `adb shell settings put global http_proxy <host>:<port>`  
   Emulator reaches host via `10.0.2.2:<port>`.
4. `network detach`: `settings put global http_proxy :0` and optional CA cleanup note.

**Emulator image matrix:**

| Image | MITM difficulty |
| --- | --- |
| Google APIs (userdebug) | Easier system CA / root |
| Google Play | Harder (no root) — rely on debug NSC |
| Physical device | Manual Wi‑Fi proxy + user CA; Phase 3.x |

Document clearly: **Play images need app-side debug trust**.

#### iOS Simulator (3.3)

1. Same mitmdump on host localhost.
2. Trust CA in simulator:
   - Open cert URL or drag install; enable Full Trust in Certificate Trust Settings — **automation is partial**.
   - Scripts: `xcrun simctl keychain` / open URL to local static file server serving the PEM — research spike mandatory.
3. Proxy: Simulator often inherits Mac network; options:
   - Set proxy via macOS network service when sim uses host network (messy).
   - Use env vars for apps that honor them (incomplete).
   - Prefer **simctl** + known working approach from Proxyman docs: install CA + configure proxy for simulator runtime.

**Spike deliverable (half-day):** spike branch notes “iOS sim proxy attach: works / partial / manual steps”. If automation < 80% reliable, ship iOS attach as **documented manual + status check**, Android fully automated.

### 3.6 Security model (non-negotiable)

1. Proxy binds **`127.0.0.1` only** by default (not `0.0.0.0`).
2. `network start` requires one of:
   - interactive confirm, or
   - `--i-understand-mitm`
3. Refuse to run if artifacts dir is world-writable oddly; umask sane.
4. CA private key never copied to device; only cert.
5. Skills text: treat MITM as privileged debug; no production pinning bypass guidance beyond “use debug build”.
6. `network stop` always attempted on `session stop` (best-effort).

### 3.7 Skills (Phase 3)

| Skill | Role |
| --- | --- |
| `mobile-network` (new) | When to start proxy; attach; mock; interpret timeline; pinning limits; redaction |
| `mobile-session` | Call `network stop` on session end; store proxy port in session.json |
| `mobile-screen` | Cross-link: reproduce UI error under mock |
| `project-router` | Load `mobile-network` when task mentions API/mock/HAR/proxy |

### 3.8 Agent workflows (acceptance stories)

**Story A — inspect login failure (Android)**

1. session start + launch app  
2. network start + attach  
3. user/agent performs login  
4. network requests list --path '*login*'  
5. screenshot + ui tree  
6. report status codes + UI error text as measured facts  

**Story B — force 500**

1. mock add login → 500  
2. replay login  
3. assert UI error state via ui find  
4. export HAR into artifacts  

**Story C — iOS (best-effort)**

Same as A if attach automated; else agent follows manual CA trust steps then continues.

### 3.9 Testing strategy (Phase 3)

| Layer | What |
| --- | --- |
| Unit | mock matcher (glob, method, first-match) |
| Unit | redaction of headers/bodies |
| Unit | flow JSONL parse/filter |
| Integration | mitmdump + local `httpx`/`urllib` client through proxy (no device) |
| Integration | mock returns 500 to client |
| Optional device | Android emulator smoke (manual/tag) |
| Fixture | sample flows.jsonl + HAR golden |

CI must **not** require emulator; device tests optional.

### 3.10 Exit criteria (Phase 3 → 0.6.0)

- [ ] `network start/stop/status` works headless with mitmdump on PATH.
- [ ] Local HTTP client through proxy records flows in JSONL.
- [ ] Mock rule forces status/body for matching URL.
- [ ] `requests list` filters by host/method/status; redacts auth headers.
- [ ] HAR export opens in a standard HAR viewer (or chrome-compatible).
- [ ] Android emulator attach script works on Google APIs image (documented).
- [ ] iOS attach: automated **or** documented manual with `network status` verifying connectivity.
- [ ] `mobile-network` skill + CAPABILITIES/USAGE/SECURITY updates.
- [ ] Session stop tears down proxy best-effort.
- [ ] Phase 1+2 regression green without mitmproxy installed (`network` commands fail gracefully with install hint).

### 3.11 Risks (Phase 3)

| Risk | Mitigation |
| --- | --- |
| HTTPS broken after partial setup | `network status` healthcheck URL; detach reverts proxy |
| Agent floods tokens with bodies | previews only; max list size |
| mitmproxy version breaks addon API | pin min version in doctor; addon uses stable hooks |
| Flutter/Dart certificate quirks | document `HttpOverrides` only as last resort in debug |
| Legal/ToS confusion | SECURITY.md: only devices/apps you own |

### 3.12 Effort estimate

| Slice | Effort |
| --- | --- |
| 3.0 core store + CLI + offline unit tests | 2–3 days |
| 3.1 Android attach | 2–3 days |
| 3.2 mocks + HAR | 1–2 days |
| 3.3 iOS attach (incl. spike) | 2–4 days |
| 3.4 skills/docs/polish | 1–2 days |
| **Total** | **~2–3 weeks** after Phase 2 |

---

## Cross-phase CLI roadmap (final shape after 0.6)

```text
autonom devices [--platform]
autonom session start|show|stop|launch|force-stop|clear
autonom ui tree|find|tap|type|key
autonom screenshot
autonom logs tail
autonom network start|stop|status|attach|detach
autonom network requests list|show
autonom network mock add|list|remove|clear
autonom network export
autonom doctor          # recommended small add in 2.3 or 3.4
```

Session.json after both phases (example):

```json
{
  "session_id": "s_abc",
  "platform": "android",
  "target_id": "emulator-5554",
  "app_id": "com.example.app",
  "artifacts_dir": ".autonom/s_abc",
  "network": {
    "enabled": true,
    "proxy_host": "127.0.0.1",
    "proxy_port": 8080,
    "device_proxy": "10.0.2.2:8080",
    "attached": true
  },
  "tooling": { "adb": "/…", "idb": null, "mitmdump": "/…" }
}
```

---

## Dependencies to install (operator)

### Phase 2
- macOS, Xcode + platform runtime
- `idb-companion` + `idb` client

### Phase 3
- `mitmproxy` / `mitmdump` on PATH (Homebrew or pipx)
- Android emulator Google APIs recommended
- Optional: `openssl` for CA inspection

`autonom doctor` (implement in 2.3 or 3.4) should report:

```text
adb: ok
simctl: ok|missing
idb: ok|missing
mitmdump: ok|missing
current session: …
network: stopped|running :8080
```

---

## Suggested milestone tags

| Tag | Contents |
| --- | --- |
| `v0.5.0-phase2` | iOS simulator parity + unified devices/session |
| `v0.6.0-phase3` | network inspect/mock + Android attach + iOS best-effort |

---

## Decision log (defaults — confirm before build)

| # | Decision | Default |
| --- | --- | --- |
| D1 | Phase order | 2 then 3 |
| D2 | iOS UI backend | idb (not raw XCUITest) |
| D3 | iOS real devices | out of 0.5.0 |
| D4 | Network engine | mitmproxy/mitmdump |
| D5 | Flow storage | JSONL under session artifacts |
| D6 | Proxy bind | 127.0.0.1 only |
| D7 | iOS network attach | spike; manual fallback OK for 0.6.0 |
| D8 | `session clear` iOS | uninstall+reinstall when install path known |
| D9 | doctor command | yes, with 2.3 or 3.4 |
| D10 | MCP wrapper | still Phase 5 (after 2+3) |

---

## Immediate next step after plan approval

1. Land **2.0 prelude PR**: `--platform`, `--target`, unified `devices`, session schema fields (Android behavior unchanged).  
2. Then **2.1–2.2** iOS backends.  
3. Tag **0.5.0**.  
4. Network **3.0** offline core before any device proxy work.
