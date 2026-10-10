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

`network stop` also detaches first. If restoration or its read-back fails, it
keeps the proxy running and the restore snapshot for retry. `session stop`
keeps the session open on a network cleanup failure, rather than reaping a
proxy the device still needs. Repeated attach preserves the first snapshot.
Android detach refreshes Wi-Fi, briefly interrupting connectivity.

An unreachable saved local proxy makes cleanup refuse with a hint naming the
exact `adb` repair; agree it with the operator instead of blindly clearing their
settings, then retry. A proxy changed outside Autonom that no longer points at
the capture is left as it is (warning `device_proxy_changed_externally`). Transparent
emulator routing requires shutting down that emulator before stopping capture.
Abrupt proxy/host crashes still need `doctor` and device-state inspection.

## Transparent capture on Android (no app change)

The workflow above sets the device's **global HTTP proxy** and trusts the CA in
the **user** store. That is enough for a native app with a debug
`network_security_config`, but it captures **nothing** from a Flutter app:
`dart:io` ignores the Android global proxy, and apps targeting API 24+ do not
trust user CAs. The fix routes the whole emulator through the proxy at launch and
puts the CA in the **system** store — with **zero changes to the app**:

`-http-proxy` is a **launch-time** flag, so pick the proxy port up front and pass
the *same* port to the boot and to `network start`. The emulator tolerates the
proxy being down at boot and connects once `network start` brings it up on that
fixed port. This is the exact order verified live on a rooted API 37 emulator:

```bash
# 1. Boot a dedicated, rootable AVD ROUTED through a chosen port (here 18091).
#    `-http-proxy` cannot be injected into an already-running emulator.
python3 <autonom-root>/scripts/autonom.py devices boot --avd Autonom_Mitm_API37 --http-proxy 127.0.0.1:18091
# -> note the serial it reports (e.g. emulator-5554); use it as <serial> below

# 2. Own a session on that emulator
python3 <autonom-root>/scripts/autonom.py session start --serial <serial> --app-id com.example.app

# 3. Start the proxy on the SAME port the emulator was routed to
python3 <autonom-root>/scripts/autonom.py network start --port 18091 --i-understand-mitm

# 4. Install the MITM CA into the SYSTEM trust store (reversible; needs root)
python3 <autonom-root>/scripts/autonom.py network attach --system-ca --i-understand-mitm --serial <serial>

# 5. Launch/drive the app, then look — Flutter/Dio requests now decrypt
python3 <autonom-root>/scripts/autonom.py session launch com.example.app
python3 <autonom-root>/scripts/autonom.py network requests list
```

`network start` is session-bound, which is why the session is owned first (step
2) and the port is fixed by hand rather than auto-chosen. `network attach
--system-ca` verifies the install from a zygote mount namespace and reports
`system_ca.verified`; a failed verify is a `system_ca_unverified` warning, not a
hard failure (HTTP is still captured, and the change is reversible).

**Hard constraint — the image must be rootable.** Use a `google_apis` system
image (userdebug: `adb root` succeeds or adbd already runs as uid 0), **not** a
`google_apis_playstore` image and not a production build — those block root, and
`network attach --system-ca` then refuses with `unsupported_capability`
(`capability: network.system_ca`). Use a **dedicated test AVD**; the change is a
tmpfs plus a bind into the zygote mount namespaces, and a **reboot clears all of
it** — never the host, never a real device. **Verified live on API >= 34** (the
APEX conscrypt store, `apex_conscrypt`); the API < 34 path uses the
`/system/etc/security/cacerts` remount instead and is **not yet verified on a
device**.

If the emulator was not booted routed to this session's proxy, `network attach
--system-ca` refuses with `unsupported_capability`
(`capability: network.transparent_capture`) and tells you to reboot with
`devices boot --avd X --http-proxy <session proxy>` — you cannot inject
`-http-proxy` into a running emulator. It also refuses (`reason:
app_proxy_attached`) while an app-proxy `network attach` is active: run `network
detach` first, so the device's previous global proxy is restored.

In transparent mode every flow on the proxy is **counted as the device's**, so
`network status` answers `attached: true` with `evidence: "transparent_proxy"`,
`target_flow_count == recent_flow_count`, and `unattributed_flow_count: 0`. The
flows all arrive from `127.0.0.1` by design, and any host process can reach the
proxy on loopback too — a `curl` through its port would be counted as well. That
is a reasonable default on a **dedicated test host**, where nothing else uses
the port; keep it that way while you gather evidence. The count holds only while
the process registry still shows this emulator booted routed through the proxy:
after `devices shutdown`, or a reboot without `--http-proxy`, `network status`
answers `attached: false` with `evidence: "transparent_route_gone"` and a
`reason` (unless a guest-network `10.0.2.x` flow still proves attachment),
and loopback flows are unattributed again. `network requests` lists
**all** captured flows. `network status` also reports `capture_mode` and the
installed `system_ca` (`verified`, `checked_via`). A fresh `adb shell ls
/apex/com.android.conscrypt/cacerts` will **not** show the cert (a different
mount namespace); Autonom verifies from a zygote namespace with `nsenter`.

The emulator's `-http-proxy` opens every tunnel as `CONNECT <ip>:443`, so the
connection target is an IP. Each flow's `host` is still the **name** the app
asked for (the Host header), and the IP is kept in `server_ip`. So `network
requests list --host backend.example.com` and `network mock add --host
backend.example.com` work as usual in transparent mode.

## Attaching, per platform

| Platform | How | Coverage |
| --- | --- | --- |
| Android emulator (transparent) | `--system-ca` on an emulator booted with `devices boot --http-proxy`: routes at launch + system-store CA | full, incl. Flutter/`dart:io` (see the section above); needs a rootable `google_apis` image |
| Android emulator (app-proxy) | sets the device's global HTTP proxy to `10.0.2.2:<port>` + user-store CA (`--install-ca`) | native apps with a debug `network_security_config`; **not** Flutter/`dart:io`, **not** pinned traffic |
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

- **Android, no app change at all:** transparent capture (see "Transparent
  capture on Android" above) — `network attach --system-ca` on a rooted emulator
  booted with `devices boot --http-proxy`. This is the only path that captures a
  release-config Flutter app without rebuilding it.
- **Preferred when you can rebuild:** point a **debug build** at a
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
