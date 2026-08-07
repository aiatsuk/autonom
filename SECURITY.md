# Security

## Scope

Use Autonom only against devices, simulators, and applications you own or are
explicitly authorized to test. Traffic interception and device configuration
changes are privileged operations regardless of how easy the tooling makes them.

## Network interception (MITM)

`autonom network` runs a mitmproxy-based man-in-the-middle proxy. The rules below
are enforced in code, not merely documented:

1. **Loopback only.** The proxy binds `127.0.0.1`. There is no flag to widen it.
   The Android emulator reaches it through `10.0.2.2`; physical devices cannot,
   and attaching them is refused rather than solved by exposing an open proxy.
2. **Explicit consent per invocation.** Starting the proxy, attaching a device, and
   installing a CA each require their own flag, plus a typed confirmation phrase on
   an interactive terminal. Consent is never cached, never inferred, and cannot be
   granted by an environment variable, a config file, or a prior grant.
3. **Credentials are redacted before they are written**, not before they are
   displayed. Sensitive headers (`authorization`, `cookie`, `set-cookie`,
   `x-api-key`, …) and credential-shaped body fields (`password`, `token`,
   `api_key`, …) are masked at capture time, so an archived artifact directory has
   never contained them.
4. **Full bodies are opt-in.** `--capture-bodies` is off by default because bodies
   are the densest source of secrets and personal data. When enabled, the command
   warns.
5. **The CA private key never enters session artifacts.** mitmproxy's confdir is a
   machine-level store (`$AUTONOM_HOME`, else `$XDG_STATE_HOME/autonom`, else
   `~/.local/state/autonom`) with mode `0700`; only `mitmproxy-ca-cert.{cer,pem}`
   are copied into a session. A test scans every artifact for private-key material.
   Keeping the CA there also makes it **stable across sessions**, so a device that
   trusted it once keeps working — a per-session CA would silently invalidate any
   certificate previously installed with `--install-ca`.
6. **Artifacts are owner-only.** The network directory is `0700` and flow files are
   `0600`; capture refuses to start into a world-writable artifacts directory.
7. **Host network settings are never changed.** Autonom does not invoke
   `networksetup` or `scutil`, and a test asserts they appear nowhere in the
   codebase. iOS attach uses per-process environment injection or documented
   manual steps.
8. **Teardown restores what was changed.** `network detach` writes back the exact
   proxy value observed at attach time — never a hard-coded default — and
   `session stop` attempts detach before stopping the proxy. `autonom doctor`
   reports a device left attached to a dead proxy.

A CA installed into a simulator's trust store is deliberately **not** removed on
detach: removing it is a second trust-store operation needing its own consent.
Clear it with `xcrun simctl keychain <udid> reset` or by erasing the simulator.

### Certificate pinning

Pinned apps defeat interception by design. Autonom does not attempt to bypass
pinning. Use a debug build with pinning relaxed; report plainly when traffic
cannot be inspected rather than working around the app's security.

## Session artifacts

`.autonom/` can contain screenshots, accessibility trees, logs, crash reports,
recordings, pulled application files, and captured traffic. Treat it as sensitive:
it is gitignored, kept out of the plugin directory, and should be deleted when an
investigation ends. App-container file access is confined to the container, and
pulled file contents are never echoed to stdout.

## Emulator browser bridge

The bridge binds `127.0.0.1` and enables a random bearer token by default. Do not
bind it to a public interface. Treat SSH or reverse-proxy exposure as privileged
device control.

## Secrets

Do not commit Android signing files, passwords, service-account credentials, Dart
defines containing secrets, or environment files. Autonom never prints keystores,
tokens, keychain contents, or `.env` values.

## Reporting

Report a security issue privately to the repository owner rather than opening a
public issue.
