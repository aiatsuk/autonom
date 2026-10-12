# Autonom probe for ChatGPT desktop (development only)

`autonom-chatgpt-probe` is a disposable development plugin. It measures what ChatGPT desktop (Work
mode) lets a local Autonom integration do: MCP tool calls, the MCP Apps bridge, model context, loopback
`fetch` and WebSocket from the app page, WebCodecs H.264 decoding and reconnects. It is **not** part of
the Autonom plugin, it is not listed in either root marketplace and it is not in the release bundle
(`scripts/build_release.sh` does not copy `integrations/`).

Layout:

| Path | What it is |
|---|---|
| `autonom-chatgpt-probe/` | The plugin: manifest, `.mcp.json`, `bin/launch.sh`, the stdio MCP server (`server/`), the probe page (`ui/`) and the loopback media endpoint (`media/`) |
| `.agents/plugins/marketplace.json` | A repo-local `autonom-dev` marketplace for `codex plugin marketplace add integrations/chatgpt` |
| `scripts/install_probe.py` | Install, uninstall and status for the personal marketplace |

## Requirements

- macOS with ChatGPT desktop, Work mode enabled for your account.
- Python 3: the server runs on Python 3.9 or newer (macOS's `/usr/bin/python3` is enough); the
  Autonom CLI and `install_probe.py` need Python 3.11 or newer.
- Node 22 or newer for the media endpoint.
- The Autonom CLI (`./scripts/install_cli.sh` in a checkout), so the probe can run `autonom version`
  and `autonom devices list`. The probe never starts an Autonom session.

## Install, status, uninstall

Run these from the repository root. `--json` prints one JSON object; `--dry-run` checks everything
and prints the planned actions without writing anything.

```sh
python3 integrations/chatgpt/scripts/install_probe.py status --json
python3 integrations/chatgpt/scripts/install_probe.py install --dry-run --json
python3 integrations/chatgpt/scripts/install_probe.py install --json \
  --allow-device android:emulator-5584 \
  --remote-route forwarded=http://127.0.0.1:47812
python3 integrations/chatgpt/scripts/install_probe.py uninstall --json [--purge-cache]
```

What `install` does:

1. Copies the plugin (a copy, not a symlink) to `~/plugins/autonom-chatgpt-probe` and writes the stamp
   `.autonom-probe-install.json` (version, source commit, file hashes, what the first install created).
2. Backs up `~/.agents/plugins/marketplace.json` byte for byte to
   `~/plugins/backups/autonom-chatgpt-probe/marketplace-<UTC time>.json`, then adds or replaces one entry
   `autonom-chatgpt-probe` (`./plugins/autonom-chatgpt-probe`). Other entries and keys keep their order
   and values. A missing marketplace is created as `personal`.
3. Writes the probe config `<state>/chatgpt-probe/config.json`, where `<state>` is `$AUTONOM_HOME`, else
   `$XDG_STATE_HOME/autonom`, else `~/.local/state/autonom`. Keys named by flags are replaced; other keys
   are kept. ChatGPT starts the server without `AUTONOM_HOME` or `XDG_STATE_HOME`, so run the script
   without them (`env -u AUTONOM_HOME -u XDG_STATE_HOME python3 ...`) when you install for real.

Re-running `install` is safe: one entry, the copy refreshed only when the source changed, and no
marketplace write (and no new backup) when the entry is already identical. Then restart ChatGPT
desktop, open Plugins, choose the Personal marketplace, install *Autonom probe* and enable it in Work
mode.

`uninstall` removes the entry (after a backup), deletes the marketplace file only when install created
it and no plugins remain, removes the copy only when its stamp is the probe's, and removes the config
only when install created it. `--purge-cache` also removes
`~/.codex/plugins/cache/<marketplace>/autonom-chatgpt-probe/` (any version folder, such as `local` or
`0.1.0`), and only when every plugin manifest inside it names the probe.

Refusals print `{"ok": false, "error_code", "error", "hint"}` and exit 1; bad flags exit 2.

| error_code | Meaning |
|---|---|
| `marketplace_invalid` | The marketplace is not a JSON object with a `plugins` list; nothing is written |
| `entry_conflict` | Another entry is named `autonom-chatgpt-probe` but points elsewhere |
| `target_conflict` | `~/plugins/autonom-chatgpt-probe` exists and was not installed by the script |
| `config_invalid` | The probe config file is not a JSON object |
| `cache_conflict` | The cache folder holds something that is not the probe |
| `device_not_allowed` | `--allow-device android:emulator-5554` (never allowed) |
| `invalid_arguments` | A flag value is out of range or malformed (exit 2) |
| `home_invalid`, `source_invalid`, `write_failed` | Bad `--home`, missing plugin tree, or a failed write |

The script never writes under `~/.codex` except `--purge-cache`, and it never edits
`~/.codex/config.toml`.

## Config keys

All keys are optional; flags of `install` write the first four.

| Key | Flag | Default |
|---|---|---|
| `media_port` | `--media-port N` (1024-65535) | `47811` |
| `remote_routes` | `--remote-route LABEL=ORIGIN` (origin `http://127.0.0.1:N` or `https://host[:port]`, at most 4) | none |
| `allow_origins` | `--allow-origin ORIGIN` (exact `scheme://host[:port]`, or one-label `https://*.suffix`) | `https://web-sandbox.oaiusercontent.com` is always allowed |
| `allowed_devices` | `--allow-device android:<serial>` or `ios:<udid>` | none |
| `autonom_cli`, `cli_python`, `node`, `autonom_root`, `adb` | edit the file (absolute paths) | found automatically |
| `cli_timeout_s` | edit the file (1-120) | `15` |

A bad value never crashes the server: it is reported as `config_error` by `probe_read`.

The media endpoint has 5 s to print its ready line. On a slow machine, such as a CI
runner, the environment variable `AUTONOM_PROBE_MEDIA_READY_TIMEOUT` (seconds, up to 600)
raises that limit.

## Security model

- The media endpoint listens on `127.0.0.1` only and checks the `Host` header (DNS rebinding).
- A new random token is made every time the media endpoint starts. It reaches the page only through
  the tool result's `_meta` (component-only), never through `structuredContent`, `content`, the HTML or
  the model context, and it is never printed or logged. The only copy on disk is the `0600` file
  `<state>/chatgpt-probe/media.json`, used to share one media endpoint between server processes.
- Page origins must match the allowlist exactly (scheme, host and port), or a configured one-label
  `https://*.suffix` wildcard. `null`, `file://` and malformed origins never match.
- Refused origins are recorded (at most 20, with counts and times) so you can see what the sandbox
  origin really is.
- Only devices listed in `allowed_devices` can be relayed; `android:emulator-5554` is always refused.

## Learning the sandbox origin

When the media checks fail with `origin_not_allowed`, ask the chat to call `probe_read` and look at
`media.refused_origins`. Allow the observed exact origin and restart ChatGPT:

```sh
python3 integrations/chatgpt/scripts/install_probe.py install --json \
  --allow-origin https://<observed host> ...same flags as before...
```

Allow exact origins only. Never allow `null` or a broad wildcard.

## Forwarded route

To test a second, declared origin for the media endpoint, forward a port with SSH and declare it as a
remote route:

```sh
ssh -N -L 47812:127.0.0.1:47811 <host>
python3 integrations/chatgpt/scripts/install_probe.py install --json \
  --remote-route forwarded=http://127.0.0.1:47812
```

Then pick the route *forwarded* in the probe page.

## Fake host harness

`tests/live/chatgpt_probe_host_live.mjs` runs the real server and page in headless Chromium and WebKit
behind a fake MCP Apps host (Playwright from `PLAYWRIGHT_ROOT`). It is not part of `node --test`:

```sh
node tests/live/chatgpt_probe_host_live.mjs --dry-run
node tests/live/chatgpt_probe_host_live.mjs --evidence-dir /tmp/probe-evidence --browser both --remote-proxy
python3 integrations/chatgpt/scripts/probe_results.py check --results /tmp/probe-evidence/results-chromium.json --profile harness
```

The unit tests for every part run in `scripts/run_checks.sh` without devices.

## Manual cleanup

`uninstall` cannot reach everything ChatGPT keeps:

1. In ChatGPT desktop, open Plugins and uninstall *Autonom probe*.
2. If `~/.codex/config.toml` still holds an `autonom-chatgpt-probe` entry, remove it by hand (the
   script never edits that file).
3. Run `install_probe.py uninstall --purge-cache` to remove the cached copy, then
   `install_probe.py status --json` to confirm nothing is left.
4. Notes and saved results stay in `<state>/chatgpt-probe/`; delete that folder if you no longer need them.
