---
name: mobile-network
description: Inspect and mock a mobile app's HTTP(S) traffic for AI agents — start a localhost MITM proxy, attach a device with explicit consent, filter recorded requests, force responses, and export HAR without leaking credentials.
---

# Mobile Network (inspect and mock)

## Purpose

Answer **what did the app actually send and receive**, and **what does it do when
the server misbehaves**. Backed by mitmproxy, bound to localhost, scoped to one
Autonom session.

Use it when the task mentions an API, a request, a response, a status code, a
mock, a HAR, a proxy, or "why is this call failing".

## Before you start: this is privileged

Starting the proxy decrypts and records traffic. Attaching a device changes that
device's network configuration, and `--install-ca` writes to its trust store.
None of that happens without **explicit consent for that exact action**:

- every privileged verb requires its flag (`--i-understand-mitm`, plus
  `--install-ca` for a trust-store write);
- on an interactive terminal you must additionally type a confirmation phrase;
- consent is **never** cached — a grant earlier in the session does not carry to
  the next command;
- there is no environment variable or config file that grants it.

Only use this against devices and apps you own or are authorized to test.

## Workflow

```bash
# 1. Own a session first
python3 <autonom-root>/scripts/autonom.py session start --serial emulator-5554 --app-id com.example.app

# 2. Start the proxy (127.0.0.1 only; port is auto-chosen when omitted)
python3 <autonom-root>/scripts/autonom.py network start --i-understand-mitm

# 3. Point the device at it
python3 <autonom-root>/scripts/autonom.py network attach --i-understand-mitm

# 4. Exercise the app, then look
python3 <autonom-root>/scripts/autonom.py network requests list --path '*/login'
python3 <autonom-root>/scripts/autonom.py network requests list --host api.example.com --status 401
python3 <autonom-root>/scripts/autonom.py network requests show f_0003

# 5. Force a failure and check the UI reacts
python3 <autonom-root>/scripts/autonom.py network mock add \
  --match '*/v1/login' --method POST --status 500 \
  --body-file fixtures/login_error.json --header 'Content-Type: application/json'
python3 <autonom-root>/scripts/autonom.py ui tap --text "Log In"
python3 <autonom-root>/scripts/autonom.py ui find --text "Something went wrong"

# 6. Keep the evidence, then clean up
python3 <autonom-root>/scripts/autonom.py network export --har network/session.har
python3 <autonom-root>/scripts/autonom.py network mock disable --all
python3 <autonom-root>/scripts/autonom.py network detach
python3 <autonom-root>/scripts/autonom.py network stop
```

Step 6 uses `disable --all`, not `clear`: the registry is persistent, so `clear`
throws away rules you may want tomorrow. Use `clear` only to actually delete them.

`session stop` performs detach and stop best-effort anyway, in that order, so the
device is never left pointing at a dead proxy.

## Attaching, per platform

| Platform | How | Coverage |
| --- | --- | --- |
| Android emulator | sets the device's global HTTP proxy to `10.0.2.2:<port>` | full |
| Android physical | **refused** — the proxy is loopback-only and a physical device cannot reach it; widening the bind would expose an open proxy | none |
| iOS Simulator | injects proxy environment variables into apps launched by `session launch` | clients honouring proxy env vars |

**iOS limitation, state it in findings:** the per-process mechanism covers
clients that read the proxy environment — curl and many SDKs. Native
`URLSession` reads the *system* proxy configuration and is **not** captured this
way. The Simulator has no proxy pane of its own — it uses the host Mac's
network stack — so the manual steps `network attach` prints set the **host's**
system proxy (and switch it off again afterwards). Ask the operator before
doing that, then confirm with `network status`. On iOS `network attach`
answers `attached: "unknown"` with `mode: manual` and `attach_state: manual`:
nothing has been observed yet, and the attach is not automatable the way
the emulator's is.

**Flutter needs an in-app hook, on both platforms.** `dart:io`'s `HttpClient`
ignores the proxy environment *and* Android's global proxy setting unless the
app itself sets `findProxy`. Add a hook to the debug build — for example
`HttpOverrides.global` with a `createHttpClient` that sets
`client.findProxy = HttpClient.findProxyFromEnvironment` (iOS), or returns
`'PROXY 10.0.2.2:<port>'` (Android emulator) — then relaunch. iOS
`network attach` says so in a `flutter_proxy_hook_required` warning. Without
the hook a Flutter app's requests never reach the proxy, whatever
`network status` says about the device.

Autonom never changes macOS network-service settings itself: that is a
system-wide change whose blast radius is the operator's whole machine.

## HTTPS and certificates

Decrypting TLS needs the app to trust the MITM CA.

- **Preferred, no CA install:** point a **debug build** at a
  `network_security_config` (Android) that trusts user CAs, or use a debug trust
  configuration on iOS.
- **iOS Simulator:** `--install-ca` runs `simctl keychain add-root-cert`, scoped to
  that one simulator.
- **Android emulator:** `--install-ca` (with `--i-understand-mitm`) runs
  `adb root` and copies the CA into the user trust store. It works only on a
  rootable `google_apis` image: a `google_apis_playstore` image blocks
  `adb root`, and the attach fails with `backend_failed` saying so. A
  physical device is refused. The same steps by hand (`/system` needs no
  remount):

  ```bash
  CA=~/.local/state/autonom/ca/mitmproxy-ca-cert.pem
  HASH=$(openssl x509 -inform PEM -subject_hash_old -in "$CA" | head -1)
  adb -s <serial> root
  adb -s <serial> push "$CA" /data/local/tmp/$HASH.0
  adb -s <serial> shell "cp /data/local/tmp/$HASH.0 /data/misc/user/0/cacerts-added/$HASH.0 \
    && chown system:system /data/misc/user/0/cacerts-added/$HASH.0 \
    && chmod 644 /data/misc/user/0/cacerts-added/$HASH.0"
  ```

  Even then the app must trust **user** CAs through its `network_security_config`.

**Certificate pinning defeats all of this by design.** If the app pins, requests
fail and there is nothing to inspect. Say so plainly and use a debug build with
pinning disabled. Do not attempt to bypass pinning in a production build.

## Reading the results

```json
{
  "id": "f_0003", "method": "POST", "url": "https://api.example.com/v1/login",
  "host": "api.example.com", "path": "/v1/login", "status": 401, "duration_ms": 42,
  "request_headers_preview": {"authorization": "<redacted>"},
  "request_body_preview": "{\"email\": \"a@b.c\", \"password\": \"<redacted>\"}",
  "mocked": false, "mock_id": null,
  "sizes": {"request_bytes": 120, "response_bytes": 80}
}
```

- **Credentials are masked before anything is written to disk** — sensitive headers
  and credential-shaped body fields (`password`, `token`, `api_key`, …). Do not
  work around this to "see the real value".
- **URLs are scrubbed too.** Values of sensitive query and fragment keys
  (`token`, `access_token`, `api_key`, `key`, `secret`, `password`, `auth`,
  `session`, `signature`, `code`, AWS signature keys, and similar — decoded
  and case-insensitive) and the password in `user:password@host` become
  `<redacted>` in the stored `url` and `Referer`, again in `requests
  list/show`, and in HAR export; other query parameters (`page=2`) stay.
- Bodies are **2 KiB previews** by default. `--capture-bodies` persists full bodies
  and is off deliberately: bodies are the densest source of secrets and personal
  data. `requests show --full` needs it and warns when used.
- Listing is capped (default 50) and reports `total_matched` and `truncated`, so a
  partial view is never mistaken for the whole story.

## Mock semantics

Rules live in a **persistent, machine-level registry**
(`~/.local/state/autonom/mocks/registry.json`), not in the session. They survive
proxy restarts, session restarts and reboots, and every session shares one set.

```bash
# One-liner: this endpoint returns this JSON.
autonom network mock add --url 'https://api.devbackend.net/post/update/12341' \
  --json '{"status":"ok"}' --note 'ticket-123 repro'

# Or a glob, when the id varies
autonom network mock add --match '*/post/update/*' --method POST --status 500

autonom network mock list [--all]        # --all includes disabled rules
autonom network mock show m_1            # rule + a scrubbed body preview
autonom network mock update m_1 --status 503
autonom network mock disable m_1 | --all # keeps the rule, stops it firing
autonom network mock enable  m_1 | --all
autonom network mock remove m_1          # deletes the rule and its body
autonom network mock clear               # deletes everything
```

- `--url` matches the endpoint **exactly** and ignores the query string, so
  `…/12341` also matches `…/12341?ts=9` but never `…/123415`. `--match` takes a
  glob and leaves query handling to you.
- Match on URL, plus optional method and host. **First enabled rule wins.**
- A matched request **never reaches the origin** — the response is manufactured,
  not rewritten after the fact.
- Rules reload without restarting the proxy, so they can be swapped mid-scenario;
  a corrupt registry keeps the last good set rather than dropping everything.
- Mock CRUD needs **no session and no device** — rules can be prepared in advance.
- Rules are validated when added or updated: a target (`--url` or `--match`)
  is required (`selector_required`), a body that looks like JSON must parse,
  `--status` must be 100..599, and each `--header` must be `Name: value`.
- The registry lives outside any repository on purpose: a mock body is often a
  captured response, and a captured response often carries a token.

### The stale-rule hazard

Persistence has a price: a rule enabled last week fires again the moment a proxy
starts, and a fabricated response looks exactly like a real one. The defence is
that everything says so — `network start`, `network status` and `doctor` all
report `mocks.active` and raise `persistent_mocks_active`.

**Before trusting any network evidence, check `mocks.active` is what you expect.**
If a response looks wrong or suspiciously convenient, run `network mock list`
before concluding anything about the backend. `requests list --mocked true` shows
exactly which flows were faked.

## Honest reporting

1. Report status codes and bodies as **measured facts**, on-screen text separately.
2. `network status` reports `attached` as `true`, `false`, or **`unknown`**,
   with the `evidence` behind it, and `attach_state` (`automated`, `manual`,
   `not_attached`) for how far the attach got. Only traffic *from the target*
   counts: a flow from the emulator's guest network (`target_flows`), else the
   device's proxy setting read back (`device_setting`); a host `curl` through
   the proxy never does. Loopback flows are unattributed
   (`unattributed_flow_count`, with `recent_user_agents` so you can judge
   them). On iOS the Simulator and the host share one network stack, so
   traffic leaves it `unknown` (`host_traffic_indistinguishable`); an Android
   device that cannot be read is `setting_unreadable`. Do not upgrade
   `unknown` to "working" in a summary.
3. A HAR exported without `--capture-bodies` carries previews; its `log.comment`
   says so. Do not present a preview as a full payload.
4. If nothing was captured, distinguish the causes: not attached, pinning, the app
   using a client that ignores the proxy, or simply no traffic yet.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `consent_required` | flag missing; on a TTY the phrase is also required |
| `proxy_not_running` | `network start` first |
| `mitmdump_required` | install mitmproxy; `autonom doctor` prints the command |
| `physical_device_attach_unsupported` | use an emulator, or configure the Wi-Fi proxy by hand |
| Requests list is empty | not attached, pinning, `URLSession` on iOS, or a Flutter app without an in-app proxy hook |
| App shows network errors after a crash | the device may still point at a dead proxy — `autonom doctor` reports it; run `network detach` |

## Related

- `mobile-session` — sessions and teardown
- `mobile-screen` — correlate a captured response with on-screen state
- `android-debugger-agent`, `ios-debugger-agent`
